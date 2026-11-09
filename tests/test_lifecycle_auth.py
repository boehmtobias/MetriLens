"""The authenticated readiness layer.

Tested against a real `http.server` rather than a mocked opener, because what the
probe has to get right is HTTP behaviour: a CSRF cookie issued on one request and
echoed on the next, and a session cookie carried to a third. A mock that returns
prepared objects would assert the shape of the probe's own calls, not that a server which
behaves like InvenTree is actually satisfied.

The handler reproduces two failure modes InvenTree shows in practice: a 403
on the login POST when no CSRF cookie was issued yet, and a login POST that
succeeds while the following session read still answers 401.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from metrilens.credentials import CredentialError
from metrilens.lifecycle import LifecycleError, ReadinessAuth, wait_for_authenticated

AUTH = ReadinessAuth(
    login_page_path="/web/login",
    login_path="/api/auth/v1/auth/login",
    session_path="/api/user/me/",
    username_env="METRILENS_TEST_USER",
    password_env="METRILENS_TEST_PASSWORD",
)


class _FakeApp:
    """State a handler shares across requests, standing in for the container.

    `cold_cycles` is how many full login cycles fail before the app starts
    behaving, which is the race: the count is what the probe has to poll through.
    """

    def __init__(self, cold_cycles: int = 0, cold_mode: str = "csrf"):
        self.cold_cycles = cold_cycles
        self.cold_mode = cold_mode
        self.requests: list[tuple[str, str]] = []
        self.logins: list[dict] = []
        self.login_headers: list[dict] = []

    @property
    def is_cold(self) -> bool:
        return self.cold_cycles > 0

    def finish_cycle(self) -> None:
        if self.cold_cycles:
            self.cold_cycles -= 1


def _handler_for(app: _FakeApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep pytest output clean
            pass

        def _respond(self, status: int, body: bytes = b"{}", cookie: str | None = None):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            app.requests.append(("GET", self.path))
            if self.path == AUTH.login_page_path:
                # A cold app in `csrf` mode issues no CSRF cookie at all, which is
                # what produced "Forbidden (CSRF cookie not set.)" in production.
                if app.is_cold and app.cold_mode == "csrf":
                    app.finish_cycle()
                    self._respond(200)
                else:
                    self._respond(200, cookie="csrftoken=token-abc; Path=/")
                return
            if self.path == AUTH.session_path:
                if app.is_cold and app.cold_mode == "session":
                    app.finish_cycle()
                    self._respond(401, b'{"detail":"unauthenticated"}')
                    return
                if "sessionid=" in self.headers.get("Cookie", ""):
                    self._respond(200, b'{"username":"admin"}')
                else:
                    self._respond(401, b'{"detail":"unauthenticated"}')
                return
            self._respond(404)

        def do_POST(self):
            app.requests.append(("POST", self.path))
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            # Lower-cased: urllib normalizes a custom header's name with
            # `capitalize()`, so `X-CSRFToken` arrives as `X-csrftoken`. Harmless
            # against a real Django app, which reads it case-insensitively out of
            # the WSGI environ, but it would make a case-sensitive assert here lie.
            app.login_headers.append({name.lower(): value for name, value in self.headers.items()})
            if self.path != AUTH.login_path:
                self._respond(404)
                return
            if not self.headers.get(AUTH.csrf_header_name):
                app.finish_cycle()
                self._respond(403, b'{"detail":"CSRF cookie not set."}')
                return
            app.logins.append(json.loads(body))
            self._respond(200, cookie="sessionid=session-xyz; Path=/")

    return Handler


@pytest.fixture
def app_server():
    """Start a `_FakeApp` on a real port, returning `(app, base_url)`."""
    servers = []

    def start(app: _FakeApp) -> str:
        server = HTTPServer(("127.0.0.1", 0), _handler_for(app))
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("METRILENS_ENV_FILE", "/nonexistent/.env")
    monkeypatch.setenv("METRILENS_TEST_USER", "admin")
    monkeypatch.setenv("METRILENS_TEST_PASSWORD", "pw")


class TestWaitForAuthenticated:
    def test_a_warm_app_clears_in_one_cycle(self, app_server):
        app = _FakeApp()
        base_url = app_server(app)
        wait_for_authenticated(f"{base_url}/api/", AUTH, timeout_s=5, poll_interval_s=0.01)
        assert app.requests == [
            ("GET", AUTH.login_page_path),
            ("POST", AUTH.login_path),
            ("GET", AUTH.session_path),
        ]

    def test_sends_the_credentials_and_echoes_the_csrf_token(self, app_server):
        app = _FakeApp()
        base_url = app_server(app)
        wait_for_authenticated(f"{base_url}/api/", AUTH, timeout_s=5, poll_interval_s=0.01)
        assert app.logins == [{"username": "admin", "password": "pw"}]
        assert app.login_headers[0][AUTH.csrf_header_name.lower()] == "token-abc"

    def test_derives_the_origin_from_the_health_url_path(self, app_server):
        # health_url is `<origin>/api/`, so the probe must strip the path, not append.
        app = _FakeApp()
        base_url = app_server(app)
        wait_for_authenticated(
            f"{base_url}/api/some/deep/path", AUTH, timeout_s=5, poll_interval_s=0.01
        )
        assert ("GET", AUTH.login_page_path) in app.requests

    def test_polls_through_a_missing_csrf_cookie(self, app_server):
        app = _FakeApp(cold_cycles=2, cold_mode="csrf")
        base_url = app_server(app)
        wait_for_authenticated(f"{base_url}/api/", AUTH, timeout_s=5, poll_interval_s=0.01)
        assert app.logins == [{"username": "admin", "password": "pw"}]
        assert app.requests.count(("GET", AUTH.login_page_path)) == 3

    def test_polls_through_a_401_on_the_session_read(self, app_server):
        app = _FakeApp(cold_cycles=1, cold_mode="session")
        base_url = app_server(app)
        wait_for_authenticated(f"{base_url}/api/", AUTH, timeout_s=5, poll_interval_s=0.01)
        assert app.requests.count(("GET", AUTH.session_path)) == 2

    def test_raises_naming_the_origin_when_it_never_clears(self, app_server):
        app = _FakeApp(cold_cycles=10_000, cold_mode="csrf")
        base_url = app_server(app)
        with pytest.raises(LifecycleError, match=f"no login against '{base_url}' succeeded"):
            wait_for_authenticated(f"{base_url}/api/", AUTH, timeout_s=0, poll_interval_s=0.01)

    def test_a_failure_does_not_leak_the_password(self, app_server):
        app = _FakeApp(cold_cycles=10_000, cold_mode="csrf")
        base_url = app_server(app)
        with pytest.raises(LifecycleError) as excinfo:
            wait_for_authenticated(f"{base_url}/api/", AUTH, timeout_s=0, poll_interval_s=0.01)
        assert "pw" not in str(excinfo.value)

    def test_an_unreachable_app_fails_as_a_lifecycle_error(self):
        # Port 1 on loopback: connection refused, so the probe must translate the
        # URLError rather than let it escape as something the orchestrator's
        # `except LifecycleError` misses and a session dies on.
        with pytest.raises(LifecycleError, match="no login against"):
            wait_for_authenticated(
                "http://127.0.0.1:1/api/", AUTH, timeout_s=0, poll_interval_s=0.01
            )

    def test_a_missing_credential_fails_before_any_request(self, app_server, monkeypatch):
        monkeypatch.delenv("METRILENS_TEST_PASSWORD", raising=False)
        app = _FakeApp()
        base_url = app_server(app)
        with pytest.raises(CredentialError, match="METRILENS_TEST_PASSWORD"):
            wait_for_authenticated(f"{base_url}/api/", AUTH, timeout_s=5, poll_interval_s=0.01)
        assert app.requests == []
