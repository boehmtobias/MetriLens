"""Reset and readiness checks for the target app's Docker Compose stack.

Full teardown-and-reseed between
repetitions (not just a restart), and the layered readiness check (compose
healthchecks where declared, the app's own HTTP endpoint, and optionally one real
authenticated round-trip).
"""

import json
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from subprocess import run
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from metrilens.credentials import require_env


class LifecycleError(RuntimeError):
    """Raised when tearing down, reseeding, or waiting for the target app fails."""


@dataclass(frozen=True)
class ReadinessAuth:
    """What the authenticated readiness probe needs to know about the target app.

    App-specific data rather than code, supplied through MetriLens's own config
    file, so `lifecycle` itself stays app-agnostic. Paths are relative to the
    origin of the version's `health_url`, which is what makes one block serve
    every `app_version` of a scenario despite each running on its own port.

    Credentials are named, never carried: `username_env`/`password_env` hold
    variable *names*, resolved through `credentials.require_env` at probe time.
    The two cookie/header names default to Django's conventions, which the
    bundled InvenTree example uses, and can be overridden for an app that differs.
    """

    login_page_path: str
    login_path: str
    session_path: str
    username_env: str
    password_env: str
    csrf_cookie_name: str = "csrftoken"
    csrf_header_name: str = "X-CSRFToken"


def _run_compose(compose_file: Path, *args: str, timeout_s: float | None = None):
    return run(
        ["docker", "compose", "-f", str(compose_file), *args],
        cwd=compose_file.parent,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def reset_target_app(compose_file: Path, reset_script: str, timeout_s: float = 120) -> None:
    """Full `down -v` + `up -d` + the version's `reset_script`.

    Brings the app to its declared known seed state before every repetition,
    real or pre-flight dry run alike, regardless of app version.
    """
    down = _run_compose(compose_file, "down", "-v", timeout_s=timeout_s)
    if down.returncode != 0:
        raise LifecycleError(f"'docker compose down -v' failed: {down.stderr.strip()}")

    up = _run_compose(compose_file, "up", "-d", timeout_s=timeout_s)
    if up.returncode != 0:
        raise LifecycleError(f"'docker compose up -d' failed: {up.stderr.strip()}")

    reset = run(
        ["/bin/sh", reset_script],
        cwd=compose_file.parent,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if reset.returncode != 0:
        raise LifecycleError(f"reset_script '{reset_script}' failed: {reset.stderr.strip()}")


def _container_health_statuses(compose_file: Path) -> dict[str, str]:
    """Health status per container, e.g. `{"inventree-server": "healthy"}`.

    A container without a declared healthcheck reports an empty string; there's
    nothing to wait on for it, so callers should treat that as always ready.
    """
    result = _run_compose(compose_file, "ps", "--format", "json")
    if result.returncode != 0:
        raise LifecycleError(f"'docker compose ps' failed: {result.stderr.strip()}")
    statuses = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        container = json.loads(line)
        statuses[container["Name"]] = container.get("Health", "")
    return statuses


def wait_for_compose_healthy(
    compose_file: Path, timeout_s: float, poll_interval_s: float = 2.0
) -> None:
    """Wait for every container with a declared healthcheck to report `healthy`.

    A no-op if the compose project declares no healthchecks at all.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        statuses = _container_health_statuses(compose_file)
        declared = {name: status for name, status in statuses.items() if status}
        if all(status == "healthy" for status in declared.values()):
            return
        if time.monotonic() >= deadline:
            raise LifecycleError(
                f"compose containers not all healthy within {timeout_s}s: {declared}"
            )
        time.sleep(poll_interval_s)


def wait_for_http_ready(url: str, timeout_s: float, poll_interval_s: float = 2.0) -> None:
    """Poll the app's own HTTP endpoint until it responds without a server error.

    Always run, even when compose healthchecks are also declared, since a
    container-level healthcheck doesn't guarantee the specific entry point the
    workload needs is actually serving correctly.
    """
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while True:
        try:
            with urlopen(url, timeout=poll_interval_s) as response:
                if response.status < 500:
                    return
                last_error = LifecycleError(f"HTTP {response.status}")
        except (URLError, OSError) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            raise LifecycleError(
                f"HTTP endpoint '{url}' not ready within {timeout_s}s: {last_error}"
            )
        time.sleep(poll_interval_s)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _authenticated_round_trip(
    origin: str, auth: ReadinessAuth, username: str, password: str
) -> None:
    """One full login cycle against `origin`, raising if any request in it fails.

    The same sequence a browser performs: fetch the login page to be issued a CSRF
    cookie, POST the credentials with that token in the header, then read the
    session endpoint. A fresh cookie jar per call, so a token from a failed cycle
    is never reused.

    `urlopen` raises `HTTPError` (a `URLError`) on any 4xx/5xx, so a not-yet-ready
    auth path (a 403 on the login POST or a 401 on the session read) surfaces as an
    exception rather than as a success with a bad status.
    """
    jar = CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))

    with opener.open(origin + auth.login_page_path, timeout=10) as response:
        response.read()
    csrf_token = next(
        (cookie.value for cookie in jar if cookie.name == auth.csrf_cookie_name), None
    )
    if csrf_token is None:
        raise LifecycleError(
            f"no '{auth.csrf_cookie_name}' cookie issued by {auth.login_page_path}"
        )

    payload = json.dumps({"username": username, "password": password}).encode()
    login_request = Request(
        origin + auth.login_path,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            auth.csrf_header_name: csrf_token,
            "Referer": origin + auth.login_page_path,
        },
    )
    with opener.open(login_request, timeout=10) as response:
        response.read()

    with opener.open(origin + auth.session_path, timeout=10) as response:
        if response.status != 200:
            raise LifecycleError(f"HTTP {response.status} from {auth.session_path}")
        response.read()


def wait_for_authenticated(
    health_url: str,
    auth: ReadinessAuth,
    timeout_s: float,
    poll_interval_s: float = 2.0,
) -> None:
    """Poll a real login cycle until one succeeds.

    The third readiness layer, and the reason for it: a freshly reset container
    answers `health_url` with 200 well before its auth path is usable, so without
    this the workload's first login would lose a timeout plus a backoff in every
    repetition, *inside* the measurement window. Paying that here instead costs
    nothing measured, since readiness runs before `metrion monitor` starts.

    Polls rather than trying once, since the auth path can become usable either
    after some time or after an earlier request has gone through.

    Never logs a resolved credential; failures name the origin and the last error.
    """
    username = require_env(auth.username_env)
    password = require_env(auth.password_env)
    origin = _origin(health_url)
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while True:
        try:
            _authenticated_round_trip(origin, auth, username, password)
            return
        except (URLError, OSError, LifecycleError) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            raise LifecycleError(
                f"no login against '{origin}' succeeded within {timeout_s}s: {last_error}"
            )
        time.sleep(poll_interval_s)


def wait_for_readiness(
    compose_file: Path,
    health_url: str,
    timeout_s: float,
    auth: ReadinessAuth | None = None,
) -> None:
    """Layered readiness check: compose healthchecks, the HTTP endpoint, then auth.

    Whichever check clears last is when the repetition starts. The authenticated
    layer runs only when the caller supplies a `ReadinessAuth`, so a target app
    without a login works exactly as it would without this layer at all.
    """
    wait_for_compose_healthy(compose_file, timeout_s)
    wait_for_http_ready(health_url, timeout_s)
    if auth is not None:
        wait_for_authenticated(health_url, auth, timeout_s)
