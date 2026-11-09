import pytest
from conftest import completed_process as _completed

from metrilens.lifecycle import (
    LifecycleError,
    ReadinessAuth,
    reset_target_app,
    wait_for_compose_healthy,
    wait_for_http_ready,
    wait_for_readiness,
)


class TestResetTargetApp:
    def test_success_runs_down_up_and_reset_script_in_order(self, monkeypatch, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}\n")
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return _completed()

        monkeypatch.setattr("metrilens.lifecycle.run", fake_run)
        reset_target_app(compose_file, "reset.sh")

        assert calls[0][:5] == ["docker", "compose", "-f", str(compose_file), "down"]
        assert calls[1][:5] == ["docker", "compose", "-f", str(compose_file), "up"]
        assert calls[2] == ["/bin/sh", "reset.sh"]

    def test_down_failure_raises_and_skips_rest(self, monkeypatch, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return _completed(returncode=1, stderr="down failed")

        monkeypatch.setattr("metrilens.lifecycle.run", fake_run)
        with pytest.raises(LifecycleError, match="down failed"):
            reset_target_app(compose_file, "reset.sh")
        assert len(calls) == 1

    def test_reset_script_failure_raises(self, monkeypatch, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"

        def fake_run(command, **kwargs):
            if command[0] == "/bin/sh":
                return _completed(returncode=1, stderr="seed failed")
            return _completed()

        monkeypatch.setattr("metrilens.lifecycle.run", fake_run)
        with pytest.raises(LifecycleError, match="seed failed"):
            reset_target_app(compose_file, "reset.sh")


class TestWaitForComposeHealthy:
    def test_no_declared_healthchecks_returns_immediately(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "metrilens.lifecycle._container_health_statuses",
            lambda compose_file: {"inventree-server": "", "inventree-db": ""},
        )
        wait_for_compose_healthy(tmp_path / "docker-compose.yml", timeout_s=1)

    def test_waits_until_healthy(self, monkeypatch, tmp_path):
        statuses = iter([{"web": "starting"}, {"web": "healthy"}])
        monkeypatch.setattr(
            "metrilens.lifecycle._container_health_statuses", lambda compose_file: next(statuses)
        )
        monkeypatch.setattr("metrilens.lifecycle.time.sleep", lambda _: None)
        wait_for_compose_healthy(tmp_path / "docker-compose.yml", timeout_s=1)

    def test_timeout_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "metrilens.lifecycle._container_health_statuses",
            lambda compose_file: {"web": "starting"},
        )
        monkeypatch.setattr("metrilens.lifecycle.time.sleep", lambda _: None)
        with pytest.raises(LifecycleError, match="not all healthy"):
            wait_for_compose_healthy(tmp_path / "docker-compose.yml", timeout_s=0)


class TestWaitForHttpReady:
    def test_success_on_first_response(self, monkeypatch):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr("metrilens.lifecycle.urlopen", lambda url, timeout: FakeResponse())
        wait_for_http_ready("http://localhost/health", timeout_s=1)

    def test_connection_error_then_timeout_raises(self, monkeypatch):
        from urllib.error import URLError

        def fake_urlopen(url, timeout):
            raise URLError("connection refused")

        monkeypatch.setattr("metrilens.lifecycle.urlopen", fake_urlopen)
        monkeypatch.setattr("metrilens.lifecycle.time.sleep", lambda _: None)
        with pytest.raises(LifecycleError, match="not ready"):
            wait_for_http_ready("http://localhost/health", timeout_s=0)


class TestWaitForReadiness:
    def test_runs_both_checks_in_order(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            "metrilens.lifecycle.wait_for_compose_healthy",
            lambda compose_file, timeout_s: calls.append("compose"),
        )
        monkeypatch.setattr(
            "metrilens.lifecycle.wait_for_http_ready",
            lambda url, timeout_s: calls.append("http"),
        )
        wait_for_readiness(tmp_path / "docker-compose.yml", "http://localhost/health", timeout_s=1)
        assert calls == ["compose", "http"]

    def test_the_auth_layer_runs_last_when_configured(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            "metrilens.lifecycle.wait_for_compose_healthy",
            lambda compose_file, timeout_s: calls.append("compose"),
        )
        monkeypatch.setattr(
            "metrilens.lifecycle.wait_for_http_ready",
            lambda url, timeout_s: calls.append("http"),
        )
        monkeypatch.setattr(
            "metrilens.lifecycle.wait_for_authenticated",
            lambda url, auth, timeout_s: calls.append("auth"),
        )
        auth = ReadinessAuth(
            login_page_path="/web/login",
            login_path="/api/login",
            session_path="/api/me",
            username_env="U",
            password_env="P",
        )
        wait_for_readiness(
            tmp_path / "docker-compose.yml", "http://localhost/health", timeout_s=1, auth=auth
        )
        assert calls == ["compose", "http", "auth"]

    def test_the_auth_layer_is_skipped_when_not_configured(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "metrilens.lifecycle.wait_for_compose_healthy", lambda compose_file, timeout_s: None
        )
        monkeypatch.setattr("metrilens.lifecycle.wait_for_http_ready", lambda url, timeout_s: None)

        def fail(*args, **kwargs):
            raise AssertionError("wait_for_authenticated must not run without a ReadinessAuth")

        monkeypatch.setattr("metrilens.lifecycle.wait_for_authenticated", fail)
        wait_for_readiness(tmp_path / "docker-compose.yml", "http://localhost/health", timeout_s=1)
