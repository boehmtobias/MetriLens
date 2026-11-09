from datetime import UTC, datetime, timedelta

import pytest
from conftest import build_step_log_from_tuples as _step_log

from metrilens.equivalence import check_equivalence
from metrilens.lifecycle import LifecycleError, ReadinessAuth
from metrilens.metrion_integration import MetrionBridgeError, MonitorKilledError
from metrilens.models import EquivalenceVerdict, Repetition, RepetitionStatus, VersionRun
from metrilens.orchestrator import (
    OrchestratorError,
    SessionConfig,
    _analyze_window,
    generate_execution_order,
    load_session_config,
    parse_idle_calibration,
    reconcile_stale_repetitions,
    run_dry_run,
    run_repetition,
    run_repetition_with_retries,
    run_session,
)
from metrilens.validation import StepLog

RAPL_IDLE_LOG = """LOG    : /some/path/rapl-idle.log
Dwell  : 60s
Begin  : 2026-07-24T10:00:54+02:00
Found  : 1 package domain(s)
Package: 0 (Package, CPU cores, GPU)
RAPL   : Starting measurement at 2026-07-24T10:01:54+02:00
=== Package 0 ===
Package Total      :  10,0000 W
Thaw   : 2026-07-24T10:02:54+02:00
"""


def _write_export(path, cpu_energy=12.3, average_power_w=0.55):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""{{
        "metadata": {{
            "filter": "x", "start_time": "2026-07-28T10:00:00", "end_time": "2026-07-28T10:00:10",
            "duration_seconds": 10.0, "unit": "J"
        }},
        "device_totals": {{"CPU": {{"total": {cpu_energy}, "idle": 1.0, "dynamic": 1.0}}}},
        "summary": {{
            "total_energy": {cpu_energy}, "idle_energy": 1.0, "dynamic_energy": 1.0,
            "average_power_w": {average_power_w}
        }}
        }}"""
    )


_SERVER_CGROUP = "/system.slice/docker-abc123def456xx.scope"
_CONTAINER_MAP = {"abc123def456": "inventree-server"}


def _write_raw_cpu_export(path, cpu_energy=12.3, intervals=12):
    """The per-thread CPU export `write_repetition` derives `cpu_energy_j` from,
    with one row inside the target stack's own cgroup.

    Spans `intervals` distinct one-second sampling intervals, padded with zero-energy rows, since
    a repetition fails when its export does not cover the window it asked for. `_config`'s
    export declares a 10 s window, so the default comfortably clears it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "interval_start,interval_end,cmdline,executable_name,resource_group_name,"
        "resource_group_id,total_energy_j,active_energy_j,idle_energy_j\n"
    )
    lines = [header, f"i0,i1,cmd,exe,{_SERVER_CGROUP},,{cpu_energy},{cpu_energy},0\n"]
    lines += [
        f"i{index},i{index + 1},cmd,exe,/system.slice/docker-000000000000xx.scope,,0,0,0\n"
        for index in range(1, intervals)
    ]
    path.write_text("".join(lines))
    return path


def _config(tmp_path, **overrides) -> SessionConfig:
    defaults = {
        "metrion_binary_path": tmp_path / "metrion",
        "metrion_config_path": tmp_path / "metrion-config.yaml",
        "metrion_output_dir": tmp_path / "metrion_output",
        "metrion_idle_log_path": tmp_path / "rapl-idle.log",
        "metrion_db_dir": tmp_path,
        "data_dir": tmp_path / "data",
        "host_descriptor": "workstation1",
        "readiness_timeout_s": 5,
        "reset_timeout_s": 5,
        "max_retries": 3,
        "settle_time_s": 0,
        # Zero here rather than the production default, so the suite does not sleep three real
        # seconds per repetition; the tests that care about the wait set it themselves.
        "monitor_tail_wait_s": 0,
    }
    defaults.update(overrides)
    return SessionConfig(**defaults)


class TestParseIdleCalibration:
    def test_parses_comma_decimal_watts(self, tmp_path):
        log_path = tmp_path / "rapl-idle.log"
        log_path.write_text(RAPL_IDLE_LOG)
        assert parse_idle_calibration(log_path) == {"package_0": 10.0}

    def test_multiple_packages(self, tmp_path):
        log_path = tmp_path / "rapl-idle.log"
        log_path.write_text(
            "=== Package 0 ===\nPackage Total      :  5,5 W\n"
            "=== Package 1 ===\nPackage Total      :  6,25 W\n"
        )
        assert parse_idle_calibration(log_path) == {"package_0": 5.5, "package_1": 6.25}

    def test_no_values_raises(self, tmp_path):
        log_path = tmp_path / "rapl-idle.log"
        log_path.write_text("nothing useful here\n")
        with pytest.raises(OrchestratorError, match="no package idle-power values"):
            parse_idle_calibration(log_path)


class TestGenerateExecutionOrder:
    def test_length_and_counts(self):
        order, seed = generate_execution_order(1, 2, repetitions_per_version=3, seed=42)
        assert len(order) == 6
        assert order.count(1) == 3
        assert order.count(2) == 3
        assert seed == 42

    def test_same_seed_is_deterministic(self):
        order_a, _ = generate_execution_order(1, 2, 4, seed=7)
        order_b, _ = generate_execution_order(1, 2, 4, seed=7)
        assert order_a == order_b

    def test_random_seed_generated_when_omitted(self):
        _, seed = generate_execution_order(1, 2, 2)
        assert isinstance(seed, int)


class TestLoadSessionConfig:
    def test_builds_config_from_yaml(self, tmp_path):
        metrion_config = tmp_path / "metrion-config.yaml"
        metrion_config.write_text("general: {}\n")
        binary = tmp_path / "metrion"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            f"metrion_binary_path: {binary}\n"
            f"metrion_config_path: {metrion_config}\n"
            f"metrion_output_dir: {output_dir}\n"
            f"metrion_idle_log_path: {tmp_path / 'rapl-idle.log'}\n"
            f"metrion_db_dir: {tmp_path}\n"
            "max_retries: 5\n"
            "readiness_timeout_s: 30\n"
            "reset_timeout_s: 90\n"
            "settle_time_s: 10\n"
        )

        config = load_session_config(config_path, data_dir=tmp_path / "data", host_descriptor="x")

        assert config.max_retries == 5
        assert config.readiness_timeout_s == 30
        assert config.reset_timeout_s == 90
        assert config.settle_time_s == 10
        assert config.metrion_config_path == metrion_config

    def _minimal_config_text(self, tmp_path) -> str:
        metrion_config = tmp_path / "metrion-config.yaml"
        metrion_config.write_text("general: {}\n")
        binary = tmp_path / "metrion"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        (tmp_path / "output").mkdir(exist_ok=True)
        return (
            f"metrion_binary_path: {binary}\n"
            f"metrion_config_path: {metrion_config}\n"
            f"metrion_output_dir: {tmp_path / 'output'}\n"
            f"metrion_idle_log_path: {tmp_path / 'rapl-idle.log'}\n"
            f"metrion_db_dir: {tmp_path}\n"
        )

    def test_readiness_auth_is_none_when_the_block_is_absent(self, tmp_path):
        # A config without the block keeps the two-layer readiness check.
        config_path = tmp_path / "config.yaml"
        config_path.write_text(self._minimal_config_text(tmp_path))
        config = load_session_config(config_path, data_dir=tmp_path / "data", host_descriptor="x")
        assert config.readiness_auth is None

    @pytest.mark.parametrize(
        ("csrf_lines", "expected_cookie", "expected_header"),
        [
            ("", "csrftoken", "X-CSRFToken"),  # Django's own defaults, block doesn't override.
            ("  csrf_cookie_name: xsrf\n  csrf_header_name: X-XSRF\n", "xsrf", "X-XSRF"),
        ],
    )
    def test_readiness_auth_is_built_from_the_block(
        self, tmp_path, csrf_lines, expected_cookie, expected_header
    ):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            self._minimal_config_text(tmp_path) + "readiness_auth:\n"
            "  login_page_path: /web/login\n"
            "  login_path: /api/auth/v1/auth/login\n"
            "  session_path: /api/user/me/\n"
            "  username_env: APP_USER\n"
            "  password_env: APP_PASSWORD\n" + csrf_lines
        )
        config = load_session_config(config_path, data_dir=tmp_path / "data", host_descriptor="x")
        assert config.readiness_auth.login_path == "/api/auth/v1/auth/login"
        assert config.readiness_auth.password_env == "APP_PASSWORD"
        assert config.readiness_auth.csrf_cookie_name == expected_cookie
        assert config.readiness_auth.csrf_header_name == expected_header

    def test_an_incomplete_readiness_auth_block_is_an_error(self, tmp_path):
        # Not a silent fallback to the old check: it would read as "auth configured"
        # while every repetition kept paying the login race.
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            self._minimal_config_text(tmp_path) + "readiness_auth:\n"
            "  login_page_path: /web/login\n"
            "  login_path: /api/auth/v1/auth/login\n"
        )
        with pytest.raises(OrchestratorError, match="session_path"):
            load_session_config(config_path, data_dir=tmp_path / "data", host_descriptor="x")


class TestReadinessAuthReachesTheReadinessCheck:
    """Both readiness call sites must forward the config's `readiness_auth`.

    Worth its own test because forgetting either one fails silently: readiness still
    passes, and the only symptom is the login race the block exists to remove
    reappearing inside the measured window.
    """

    _AUTH = ReadinessAuth(
        login_page_path="/web/login",
        login_path="/api/login",
        session_path="/api/me",
        username_env="U",
        password_env="P",
    )

    def _record_readiness(self, monkeypatch) -> list:
        seen: list = []
        monkeypatch.setattr(
            "metrilens.orchestrator.wait_for_readiness",
            lambda compose_file, health_url, timeout_s, auth=None: seen.append(auth),
        )
        return seen

    def test_the_dry_run_forwards_it(self, monkeypatch, tmp_path, app_versions):
        version_a, _ = app_versions
        seen = self._record_readiness(monkeypatch)
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)

        def fake_run_script(script_path, output_path, extra_env=None):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(_step_log([("create_part", 1, 1)]).model_dump_json())

        monkeypatch.setattr("metrilens.orchestrator._run_script", fake_run_script)
        run_dry_run(version_a, _config(tmp_path, readiness_auth=self._AUTH), tmp_path / "dry")
        assert seen == [self._AUTH]

    def test_a_repetition_forwards_it(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        seen = self._record_readiness(monkeypatch)
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)

        # Stop right after readiness: a failing PID resolution ends the repetition,
        # and what is under test is the argument readiness received before that.
        def stop_here(*a, **k):
            raise MetrionBridgeError("stop here")

        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", stop_here)
        run_repetition(
            session,
            version_run,
            version_a,
            0,
            _config(tmp_path, readiness_auth=self._AUTH),
            tmp_path / "run",
        )
        assert seen == [self._AUTH]


class TestAnalyzeWindow:
    """The window `metrion analyze` measures.

    Narrowed to the declared steps so the scenario script's own startup, browser launch, and
    login stay out of the reported energy. That overhead can be a large share of a short window
    and cannot be removed from the process window itself. Rounded outward, because `analyze`
    exports only the intervals it fully contains.
    """

    def _write_step_log(self, tmp_path, start, end):
        path = tmp_path / "step_log.json"
        path.write_text(
            StepLog(
                start_time=start,
                end_time=end,
                step_count=1,
                action_count=1,
                entity_count=1,
                steps=[
                    {
                        "step_id": "create_part",
                        "start_time": start.isoformat(),
                        "end_time": end.isoformat(),
                        "action_count": 1,
                        "entities": [{"entity_type": "part", "identifier": "x"}],
                    }
                ],
            ).model_dump_json()
        )
        return path

    def test_rounds_outward_so_the_boundary_intervals_survive(self, tmp_path):
        """`analyze` exports only intervals lying entirely inside the requested window, never
        one that straddles a boundary, so an inward boundary would drop the interval containing
        it along with the real workload in it. Rounding outward keeps those intervals instead,
        and the rounded window still covers the whole step span it was built from, stated as its
        own assertion rather than left implicit in the exact bounds above."""
        process_start = datetime(2026, 8, 11, 22, 0, 0, tzinfo=UTC)
        process_end = datetime(2026, 8, 11, 22, 0, 50, tzinfo=UTC)
        step_start = datetime(2026, 8, 11, 22, 0, 28, 764756, tzinfo=UTC)
        step_end = datetime(2026, 8, 11, 22, 0, 49, 161050, tzinfo=UTC)
        path = self._write_step_log(tmp_path, step_start, step_end)

        start, end, note = _analyze_window(path, process_start, process_end)

        assert note is None
        assert start == datetime(2026, 8, 11, 22, 0, 28, tzinfo=UTC)
        assert end == datetime(2026, 8, 11, 22, 0, 50, tzinfo=UTC)
        assert start <= step_start and end >= step_end

    def test_leaves_whole_second_bounds_alone(self, tmp_path):
        process_start = datetime(2026, 8, 11, 22, 0, 0, tzinfo=UTC)
        process_end = datetime(2026, 8, 11, 22, 0, 50, tzinfo=UTC)
        step_start = datetime(2026, 8, 11, 22, 0, 28, tzinfo=UTC)
        step_end = datetime(2026, 8, 11, 22, 0, 49, tzinfo=UTC)
        path = self._write_step_log(tmp_path, step_start, step_end)

        start, end, note = _analyze_window(path, process_start, process_end)

        assert (start, end, note) == (step_start, step_end, None)

    def test_falls_back_when_the_step_span_is_two_hours_off(self, tmp_path):
        """The guard that matters most.

        A step log in naive local time can be two hours off. Such an offset would ask
        METRION for a window containing no samples, which yields an empty export and fails the
        repetition through a message about missing files rather than about the clock.
        """
        process_start = datetime(2026, 8, 11, 22, 0, 0, tzinfo=UTC)
        process_end = datetime(2026, 8, 11, 22, 0, 50, tzinfo=UTC)
        path = self._write_step_log(
            tmp_path,
            datetime(2026, 8, 12, 0, 0, 28, tzinfo=UTC),
            datetime(2026, 8, 12, 0, 0, 49, tzinfo=UTC),
        )

        start, end, note = _analyze_window(path, process_start, process_end)

        assert (start, end) == (process_start, process_end)
        assert "does not lie inside the process window" in note

    def test_falls_back_when_the_step_log_is_missing(self, tmp_path):
        process_start = datetime(2026, 8, 11, 22, 0, 0, tzinfo=UTC)
        process_end = datetime(2026, 8, 11, 22, 0, 50, tzinfo=UTC)

        start, end, note = _analyze_window(tmp_path / "absent.json", process_start, process_end)

        assert (start, end) == (process_start, process_end)
        assert "step log unreadable" in note

    def test_a_sub_second_span_now_yields_one_whole_second(self, tmp_path):
        """Under inward truncation this collapsed and fell back to the whole process window.

        Rounding outward has no such case: flooring the start and ceiling the end of any span
        with `start < end` leaves at least a second, and that second contains the span.
        """
        process_start = datetime(2026, 8, 11, 22, 0, 0, tzinfo=UTC)
        step_start = datetime(2026, 8, 11, 22, 0, 28, 200000, tzinfo=UTC)
        step_end = datetime(2026, 8, 11, 22, 0, 28, 900000, tzinfo=UTC)
        path = self._write_step_log(tmp_path, step_start, step_end)

        start, end, note = _analyze_window(
            path, process_start, datetime(2026, 8, 11, 22, 0, 50, tzinfo=UTC)
        )

        assert note is None
        assert (start, end) == (
            datetime(2026, 8, 11, 22, 0, 28, tzinfo=UTC),
            datetime(2026, 8, 11, 22, 0, 29, tzinfo=UTC),
        )


class TestRunRepetition:
    def _patch_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.wait_for_readiness", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", lambda *a, **k: [123])
        monkeypatch.setattr(
            "metrilens.orchestrator.resolve_compose_container_map", lambda *a, **k: _CONTAINER_MAP
        )
        monkeypatch.setattr("metrilens.orchestrator.start_monitor", lambda *a, **k: "proc")
        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", lambda *a, **k: None)

        def fake_run_script(script_path, output_path):
            log = _step_log([("create_part", 1, 1)])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(log.model_dump_json())

        monkeypatch.setattr("metrilens.orchestrator._run_script", fake_run_script)
        monkeypatch.setattr("metrilens.orchestrator.shutil.copy2", lambda *a, **k: None)
        monkeypatch.setattr(
            "metrilens.orchestrator.find_new_db_file", lambda *a, **k: tmp_path / "monitor.db"
        )

        export_path = tmp_path / "archive" / "energy_stats_x.json"
        _write_export(export_path)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "archive" / "raw_cpu_x.csv")

        def fake_run_analyze(*a, **k):
            return {"energy_stats_x.json": export_path, "raw_cpu_x.csv": raw_cpu_path}

        monkeypatch.setattr("metrilens.orchestrator.run_analyze", fake_run_analyze)

    def test_success_completes_repetition(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        self._patch_success(monkeypatch, tmp_path)
        config = _config(tmp_path)

        repetition = run_repetition(session, version_run, version_a, 0, config, tmp_path / "run")

        assert repetition.status is RepetitionStatus.OK
        assert repetition.cpu_energy_j == 12.3

    def test_analyze_receives_the_narrowed_window(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        """Whatever `_analyze_window` decides is what METRION is asked to measure.

        Asserted through the seam rather than end to end, because the process window is real
        wall-clock time here and a step log written inside it cannot be pinned deterministically.
        """
        version_a, _ = app_versions
        self._patch_success(monkeypatch, tmp_path)
        narrowed = (
            datetime(2026, 8, 11, 22, 0, 29, tzinfo=UTC),
            datetime(2026, 8, 11, 22, 0, 49, tzinfo=UTC),
        )
        monkeypatch.setattr(
            "metrilens.orchestrator._analyze_window",
            lambda step_log_path, process_start, process_end: (*narrowed, None),
        )
        seen = {}

        def recording_analyze(binary, db_path, start_time, end_time, output_dir, run_dir):
            seen["window"] = (start_time, end_time)
            return {
                "energy_stats_x.json": tmp_path / "archive" / "energy_stats_x.json",
                "raw_cpu_x.csv": tmp_path / "archive" / "raw_cpu_x.csv",
            }

        monkeypatch.setattr("metrilens.orchestrator.run_analyze", recording_analyze)

        repetition = run_repetition(
            session, version_run, version_a, 0, _config(tmp_path), tmp_path / "run"
        )

        assert seen["window"] == narrowed
        assert repetition.status is RepetitionStatus.OK

    def test_a_window_fallback_is_recorded_on_the_row(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        """A silent fallback would be the worst outcome: the row would look clean while its
        energy covered the whole process, including browser startup and login."""
        version_a, _ = app_versions
        self._patch_success(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "metrilens.orchestrator._analyze_window",
            lambda step_log_path, process_start, process_end: (
                process_start,
                process_end,
                "analyze window: step log unreadable (test)",
            ),
        )

        repetition = run_repetition(
            session, version_run, version_a, 0, _config(tmp_path), tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.OK
        assert "step log unreadable (test)" in repetition.validation_notes

    def test_lifecycle_failure_fails_measurement(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions

        def fail_reset(*a, **k):
            raise LifecycleError("boom")

        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", fail_reset)
        config = _config(tmp_path)

        repetition = run_repetition(session, version_run, version_a, 0, config, tmp_path / "run")

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "lifecycle failure" in repetition.validation_notes

    def test_script_failure_fails_measurement(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.wait_for_readiness", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", lambda *a, **k: [123])
        monkeypatch.setattr(
            "metrilens.orchestrator.resolve_compose_container_map", lambda *a, **k: _CONTAINER_MAP
        )
        monkeypatch.setattr("metrilens.orchestrator.start_monitor", lambda *a, **k: "proc")
        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", lambda *a, **k: None)

        def fail_run_script(*a, **k):
            raise OrchestratorError("script exited 1")

        monkeypatch.setattr("metrilens.orchestrator._run_script", fail_run_script)
        config = _config(tmp_path)

        repetition = run_repetition(session, version_run, version_a, 0, config, tmp_path / "run")

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "script failure" in repetition.validation_notes

    def test_script_failure_survives_a_stop_monitor_failure_during_cleanup(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        """A script failure's cleanup call to `stop_monitor` can itself raise
        `MetrionBridgeError` (METRION's monitor not exiting within its timeout after SIGINT).
        Uncaught, it would skip `run_repetition_with_retries`'s retry loop, which only retries a
        *returned* failed repetition, and end the whole session. `stop_monitor` has already
        escalated to SIGKILL and swept orphaned workers by the time it raises, so the repetition
        fails instead of the session.
        """
        version_a, _ = app_versions
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.wait_for_readiness", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", lambda *a, **k: [123])
        monkeypatch.setattr(
            "metrilens.orchestrator.resolve_compose_container_map", lambda *a, **k: _CONTAINER_MAP
        )
        monkeypatch.setattr("metrilens.orchestrator.start_monitor", lambda *a, **k: "proc")

        def fail_stop_monitor(*a, **k):
            raise MetrionBridgeError(
                "'metrion monitor' did not exit within 90s after SIGINT, killed"
            )

        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", fail_stop_monitor)

        def fail_run_script(*a, **k):
            raise OrchestratorError("Locator.click: Timeout 30000ms exceeded")

        monkeypatch.setattr("metrilens.orchestrator._run_script", fail_run_script)
        config = _config(tmp_path)

        repetition = run_repetition(session, version_run, version_a, 0, config, tmp_path / "run")

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "script failure" in repetition.validation_notes
        assert "metrion monitor stop failure" in repetition.validation_notes

    def test_analyze_failure_fails_measurement(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.wait_for_readiness", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", lambda *a, **k: [123])
        monkeypatch.setattr(
            "metrilens.orchestrator.resolve_compose_container_map", lambda *a, **k: _CONTAINER_MAP
        )
        monkeypatch.setattr("metrilens.orchestrator.start_monitor", lambda *a, **k: "proc")
        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.shutil.copy2", lambda *a, **k: None)
        monkeypatch.setattr(
            "metrilens.orchestrator.find_new_db_file", lambda *a, **k: tmp_path / "monitor.db"
        )

        def fake_run_script(script_path, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(_step_log([("create_part", 1, 1)]).model_dump_json())

        monkeypatch.setattr("metrilens.orchestrator._run_script", fake_run_script)

        def fail_analyze(*a, **k):
            raise MetrionBridgeError("analyze failed")

        monkeypatch.setattr("metrilens.orchestrator.run_analyze", fail_analyze)
        config = _config(tmp_path)

        repetition = run_repetition(session, version_run, version_a, 0, config, tmp_path / "run")

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "metrion analyze failure" in repetition.validation_notes


class TestMonitorTailWait:
    """METRION loses its in-flight interval when stopped, so keep sampling first."""

    def test_the_wait_happens_after_the_script_and_before_the_monitor_stops(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        TestRunRepetition()._patch_success(monkeypatch, tmp_path)
        events: list[str] = []
        monkeypatch.setattr(
            "metrilens.orchestrator._run_script",
            lambda script_path, output_path: (
                events.append("script"),
                output_path.parent.mkdir(parents=True, exist_ok=True),
                output_path.write_text(_step_log([("create_part", 1, 1)]).model_dump_json()),
            ),
        )
        monkeypatch.setattr(
            "metrilens.orchestrator.time.sleep", lambda seconds: events.append(f"slept {seconds}")
        )
        monkeypatch.setattr(
            "metrilens.orchestrator.stop_monitor", lambda *a, **k: events.append("stop")
        )
        config = _config(tmp_path, monitor_tail_wait_s=2.5)

        run_repetition(session, version_run, version_a, 0, config, tmp_path / "run")

        assert events == ["script", "slept 2.5", "stop"]

    def test_a_failed_script_stops_the_monitor_without_waiting(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        """Nothing is measured on that repetition, so the seconds would buy nothing."""
        version_a, _ = app_versions
        TestRunRepetition()._patch_success(monkeypatch, tmp_path)
        slept: list[float] = []
        monkeypatch.setattr("metrilens.orchestrator.time.sleep", slept.append)

        def fail_script(script_path, output_path):
            raise OrchestratorError("script exited 1")

        monkeypatch.setattr("metrilens.orchestrator._run_script", fail_script)

        repetition = run_repetition(
            session, version_run, version_a, 0, _config(tmp_path), tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert slept == []


class TestReconcileStaleRepetitions:
    """A session that died mid-repetition leaves a row nothing else can move."""

    def _running(self, session, version_run, started_at: datetime | None) -> Repetition:
        repetition = Repetition(
            version_run_id=version_run.id,
            repetition_index=0,
            measurement_started_at=started_at,
        )
        session.add(repetition)
        session.commit()
        assert repetition.status is RepetitionStatus.RUNNING
        return repetition

    def test_abandoned_running_row_is_failed_with_its_age(self, session, version_run):
        repetition = self._running(
            session, version_run, datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=3)
        )

        reconciled = reconcile_stale_repetitions(session)

        assert [row.id for row in reconciled] == [repetition.id]
        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "session abandoned" in repetition.validation_notes
        assert "10800 s before this session" in repetition.validation_notes

    def test_a_missing_start_timestamp_still_gets_reconciled(self, session, version_run):
        """The age is a courtesy; being stuck is the thing that has to be fixed."""
        repetition = self._running(session, version_run, None)

        reconcile_stale_repetitions(session)

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "measurement_started_at is not set" in repetition.validation_notes

    def test_an_aware_timestamp_does_not_break_the_age(self, session, version_run):
        """SQLite drops `tzinfo` on write, but an unflushed row in memory can still carry one."""
        repetition = Repetition(
            version_run_id=version_run.id,
            repetition_index=0,
            measurement_started_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        session.add(repetition)

        reconcile_stale_repetitions(session)

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "300 s before this session" in repetition.validation_notes

    def test_terminal_rows_are_left_alone(self, session, version_run):
        finished = self._running(session, version_run, datetime(2026, 8, 11, 2, 0, 0))
        finished.complete()
        session.commit()

        assert reconcile_stale_repetitions(session) == []
        assert finished.status is RepetitionStatus.OK
        assert finished.validation_notes is None

    def test_run_session_reconciles_before_the_gate_halts_it(
        self, monkeypatch, tmp_path, session, scenario, app_versions, version_run
    ):
        """Ahead of the pre-flight gate, so a halted session still leaves the database tidy."""
        version_a, version_b = app_versions
        abandoned = self._running(
            session, version_run, datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
        )
        step_logs = iter(
            [
                _step_log([("create_part", 1, 1)]),
                _step_log([("create_part", 1, 1), ("adjust_stock", 1, 1)]),
            ]
        )
        monkeypatch.setattr("metrilens.orchestrator.run_dry_run", lambda *a, **k: next(step_logs))

        with pytest.raises(OrchestratorError, match="FLAGGED"):
            run_session(session, scenario, version_a, version_b, 1, _config(tmp_path), seed=1)

        assert abandoned.status is RepetitionStatus.FAILED_MEASUREMENT


class TestFailedAttemptArchiving:
    """A failed attempt's METRION database is archived rather than left in METRION's own working
    directory, which nothing sweeps. It is the sole record of a failed monitor run."""

    def _patch_up_to_the_script(self, monkeypatch, tmp_path, metrion_db):
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.wait_for_readiness", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", lambda *a, **k: [123])
        monkeypatch.setattr(
            "metrilens.orchestrator.resolve_compose_container_map", lambda *a, **k: _CONTAINER_MAP
        )
        monkeypatch.setattr("metrilens.orchestrator.start_monitor", lambda *a, **k: "proc")
        monkeypatch.setattr("metrilens.orchestrator.find_new_db_file", lambda *a, **k: metrion_db)

    def test_a_script_failure_still_archives_the_database(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        metrion_db = tmp_path / "monitor.db"
        metrion_db.write_text("collected samples")
        self._patch_up_to_the_script(monkeypatch, tmp_path, metrion_db)
        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", lambda *a, **k: None)

        def failing_script(script_path, output_path):
            raise OrchestratorError("build order timed out")

        monkeypatch.setattr("metrilens.orchestrator._run_script", failing_script)
        run_dir = tmp_path / "run"

        repetition = run_repetition(session, version_run, version_a, 0, _config(tmp_path), run_dir)

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert (run_dir / "metrion.db").read_text() == "collected samples"

    def test_a_stop_failure_still_archives_the_database(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        """A monitor killed after the grace period often has a database holding the complete
        window."""
        version_a, _ = app_versions
        metrion_db = tmp_path / "monitor.db"
        metrion_db.write_text("complete window")
        self._patch_up_to_the_script(monkeypatch, tmp_path, metrion_db)

        def failing_stop(*a, **k):
            raise MetrionBridgeError("'metrion monitor' (pid 1) did not exit within 180s, killed")

        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", failing_stop)

        def fake_run_script(script_path, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(_step_log([("create_part", 1, 1)]).model_dump_json())

        monkeypatch.setattr("metrilens.orchestrator._run_script", fake_run_script)
        run_dir = tmp_path / "run"

        repetition = run_repetition(session, version_run, version_a, 0, _config(tmp_path), run_dir)

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "did not exit within 180s" in repetition.validation_notes
        assert (run_dir / "metrion.db").read_text() == "complete window"

    def test_an_archiving_failure_never_masks_the_original_reason(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        self._patch_up_to_the_script(monkeypatch, tmp_path, tmp_path / "monitor.db")
        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", lambda *a, **k: None)
        monkeypatch.setattr(
            "metrilens.orchestrator.find_new_db_file",
            lambda *a, **k: (_ for _ in ()).throw(MetrionBridgeError("no .db file in dir")),
        )

        def failing_script(script_path, output_path):
            raise OrchestratorError("build order timed out")

        monkeypatch.setattr("metrilens.orchestrator._run_script", failing_script)

        repetition = run_repetition(
            session, version_run, version_a, 0, _config(tmp_path), tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "build order timed out" in repetition.validation_notes
        assert "could not be archived" in repetition.validation_notes


class TestKilledMonitorSurvives:
    """METRION's shutdown is often slow rather than hung, so by the time the grace period expires
    it has usually written the whole window. Discarding those repetitions would waste them, so the
    export decides instead."""

    def _patch_success(self, monkeypatch, tmp_path, stop):
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.wait_for_readiness", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", lambda *a, **k: [123])
        monkeypatch.setattr(
            "metrilens.orchestrator.resolve_compose_container_map", lambda *a, **k: _CONTAINER_MAP
        )
        monkeypatch.setattr("metrilens.orchestrator.start_monitor", lambda *a, **k: "proc")
        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", stop)
        monkeypatch.setattr("metrilens.orchestrator.shutil.copy2", lambda *a, **k: None)
        monkeypatch.setattr(
            "metrilens.orchestrator.find_new_db_file", lambda *a, **k: tmp_path / "monitor.db"
        )

        def fake_run_script(script_path, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(_step_log([("create_part", 1, 1)]).model_dump_json())

        monkeypatch.setattr("metrilens.orchestrator._run_script", fake_run_script)

    def _patch_analyze(self, monkeypatch, tmp_path, intervals):
        export_path = tmp_path / "archive" / "energy_stats_x.json"
        _write_export(export_path)
        raw_cpu_path = _write_raw_cpu_export(
            tmp_path / "archive" / "raw_cpu_x.csv", intervals=intervals
        )
        monkeypatch.setattr(
            "metrilens.orchestrator.run_analyze",
            lambda *a, **k: {
                "energy_stats_x.json": export_path,
                "raw_cpu_x.csv": raw_cpu_path,
            },
        )

    def _killed(self, *a, **k):
        raise MonitorKilledError(
            "'metrion monitor' (pid 1) did not exit within 180s after SIGINT, killed"
        )

    def test_a_complete_window_survives_the_kill_and_says_so(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        self._patch_success(monkeypatch, tmp_path, self._killed)
        self._patch_analyze(monkeypatch, tmp_path, intervals=12)

        repetition = run_repetition(
            session, version_run, version_a, 0, _config(tmp_path), tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.OK
        assert repetition.cpu_energy_j == 12.3
        assert "killed after its grace period" in repetition.validation_notes

    def test_a_partly_measured_window_still_fails(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        """The gate that makes the change safe: a monitor killed during startup leaves a fraction of
        the window, and that has to fail rather than land as a plausible-looking low reading."""
        version_a, _ = app_versions
        self._patch_success(monkeypatch, tmp_path, self._killed)
        self._patch_analyze(monkeypatch, tmp_path, intervals=2)

        repetition = run_repetition(
            session, version_run, version_a, 0, _config(tmp_path), tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.FAILED_VALIDATION
        assert "requested seconds" in repetition.validation_notes

    def test_any_other_stop_failure_still_fails_the_repetition(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        """A non-zero exit after a clean stop says nothing about the data, so it keeps failing."""
        version_a, _ = app_versions

        def other_failure(*a, **k):
            raise MetrionBridgeError("'metrion monitor' exited 1 after stop signal: boom")

        self._patch_success(monkeypatch, tmp_path, other_failure)
        self._patch_analyze(monkeypatch, tmp_path, intervals=12)

        repetition = run_repetition(
            session, version_run, version_a, 0, _config(tmp_path), tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert "exited 1 after stop signal" in repetition.validation_notes


class TestRunRepetitionWithRetries:
    def test_exhausts_retries_and_stays_failed(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions

        def fail_reset(*a, **k):
            raise LifecycleError("always fails")

        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", fail_reset)
        config = _config(tmp_path, max_retries=2)

        repetition = run_repetition_with_retries(
            session, version_run, version_a, 0, config, tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.FAILED_MEASUREMENT
        assert repetition.attempt_count == 2

    def test_succeeds_after_one_retry(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        attempts = {"n": 0}

        def flaky_reset(*a, **k):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise LifecycleError("first attempt fails")

        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", flaky_reset)
        monkeypatch.setattr("metrilens.orchestrator.wait_for_readiness", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", lambda *a, **k: [123])
        monkeypatch.setattr(
            "metrilens.orchestrator.resolve_compose_container_map", lambda *a, **k: _CONTAINER_MAP
        )
        monkeypatch.setattr("metrilens.orchestrator.start_monitor", lambda *a, **k: "proc")
        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.shutil.copy2", lambda *a, **k: None)
        monkeypatch.setattr(
            "metrilens.orchestrator.find_new_db_file", lambda *a, **k: tmp_path / "monitor.db"
        )

        def fake_run_script(script_path, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(_step_log([("create_part", 1, 1)]).model_dump_json())

        monkeypatch.setattr("metrilens.orchestrator._run_script", fake_run_script)
        export_path = tmp_path / "archive" / "energy_stats_x.json"
        _write_export(export_path)
        monkeypatch.setattr(
            "metrilens.orchestrator.run_analyze",
            lambda *a, **k: {
                "energy_stats_x.json": export_path,
                "raw_cpu_x.csv": _write_raw_cpu_export(tmp_path / "archive" / "raw_cpu_x.csv"),
            },
        )
        config = _config(tmp_path, max_retries=3)

        repetition = run_repetition_with_retries(
            session, version_run, version_a, 0, config, tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.OK
        assert repetition.attempt_count == 1

    def test_a_successful_retry_keeps_the_failed_attempt_s_reason(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        """Every retry re-enters `_execute_repetition`, which overwrites `validation_notes`, so a
        repetition that failed and then succeeded could end as `ok` with the column null. The
        surviving row must say what it cost to get the measurement."""
        version_a, _ = app_versions
        attempts = {"n": 0}

        def flaky_reset(*a, **k):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise LifecycleError(f"boom {attempts['n']}")

        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", flaky_reset)
        self._patch_success_after_reset(monkeypatch, tmp_path)

        repetition = run_repetition_with_retries(
            session, version_run, version_a, 0, _config(tmp_path, max_retries=3), tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.OK
        assert repetition.attempt_count == 2
        assert "attempt 1 failed: lifecycle failure: boom 1" in repetition.validation_notes
        assert "attempt 2 failed: lifecycle failure: boom 2" in repetition.validation_notes

    def test_no_history_note_when_the_first_attempt_succeeds(
        self, monkeypatch, tmp_path, session, version_run, app_versions
    ):
        version_a, _ = app_versions
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)
        self._patch_success_after_reset(monkeypatch, tmp_path)

        repetition = run_repetition_with_retries(
            session, version_run, version_a, 0, _config(tmp_path), tmp_path / "run"
        )

        assert repetition.status is RepetitionStatus.OK
        # Any note here is the window fallback this fixture's step log triggers, never an attempt
        # history: nothing failed, so there is no earlier attempt to carry forward.
        assert "attempt" not in (repetition.validation_notes or "")

    def _patch_success_after_reset(self, monkeypatch, tmp_path):
        monkeypatch.setattr("metrilens.orchestrator.wait_for_readiness", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", lambda *a, **k: [123])
        monkeypatch.setattr(
            "metrilens.orchestrator.resolve_compose_container_map", lambda *a, **k: _CONTAINER_MAP
        )
        monkeypatch.setattr("metrilens.orchestrator.start_monitor", lambda *a, **k: "proc")
        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.shutil.copy2", lambda *a, **k: None)
        monkeypatch.setattr(
            "metrilens.orchestrator.find_new_db_file", lambda *a, **k: tmp_path / "monitor.db"
        )

        def fake_run_script(script_path, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(_step_log([("create_part", 1, 1)]).model_dump_json())

        monkeypatch.setattr("metrilens.orchestrator._run_script", fake_run_script)
        export_path = tmp_path / "archive" / "energy_stats_x.json"
        _write_export(export_path)
        monkeypatch.setattr(
            "metrilens.orchestrator.run_analyze",
            lambda *a, **k: {
                "energy_stats_x.json": export_path,
                "raw_cpu_x.csv": _write_raw_cpu_export(tmp_path / "archive" / "raw_cpu_x.csv"),
            },
        )


class TestRunSession:
    def _patch_all_success(self, monkeypatch, tmp_path):
        monkeypatch.setattr("metrilens.orchestrator.reset_target_app", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.wait_for_readiness", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.resolve_compose_pids", lambda *a, **k: [123])
        monkeypatch.setattr(
            "metrilens.orchestrator.resolve_compose_container_map", lambda *a, **k: _CONTAINER_MAP
        )
        monkeypatch.setattr("metrilens.orchestrator.start_monitor", lambda *a, **k: "proc")
        monkeypatch.setattr("metrilens.orchestrator.stop_monitor", lambda *a, **k: None)
        monkeypatch.setattr("metrilens.orchestrator.shutil.copy2", lambda *a, **k: None)
        monkeypatch.setattr(
            "metrilens.orchestrator.find_new_db_file", lambda *a, **k: tmp_path / "monitor.db"
        )

        def fake_run_script(script_path, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(_step_log([("create_part", 1, 1)]).model_dump_json())

        monkeypatch.setattr("metrilens.orchestrator._run_script", fake_run_script)

        export_path = tmp_path / "archive" / "energy_stats_x.json"
        _write_export(export_path)
        monkeypatch.setattr(
            "metrilens.orchestrator.run_analyze",
            lambda *a, **k: {
                "energy_stats_x.json": export_path,
                "raw_cpu_x.csv": _write_raw_cpu_export(tmp_path / "archive" / "raw_cpu_x.csv"),
            },
        )

        idle_log_path = tmp_path / "rapl-idle.log"
        idle_log_path.write_text(RAPL_IDLE_LOG)
        monkeypatch.setattr(
            "metrilens.orchestrator.force_idle_calibration", lambda *a, **k: idle_log_path
        )

    def test_flagged_preflight_halts_before_measurement(
        self, monkeypatch, tmp_path, session, scenario, app_versions
    ):
        version_a, version_b = app_versions
        self._patch_all_success(monkeypatch, tmp_path)

        step_logs = iter(
            [
                _step_log([("create_part", 1, 1)]),
                _step_log([("create_part", 1, 1), ("adjust_stock", 1, 1)]),
            ]
        )
        monkeypatch.setattr("metrilens.orchestrator.run_dry_run", lambda *a, **k: next(step_logs))
        config = _config(tmp_path)

        with pytest.raises(OrchestratorError, match="FLAGGED"):
            run_session(session, scenario, version_a, version_b, 1, config, seed=1)

    def test_dry_run_lifecycle_failure_raises_orchestrator_error(
        self, monkeypatch, tmp_path, session, scenario, app_versions
    ):
        """A `LifecycleError` during the pre-flight dry run must halt cleanly via
        `OrchestratorError`, not escape uncaught and crash the CLI."""
        version_a, version_b = app_versions
        self._patch_all_success(monkeypatch, tmp_path)

        def failing_dry_run(*args, **kwargs):
            raise LifecycleError("compose stack never became healthy")

        monkeypatch.setattr("metrilens.orchestrator.run_dry_run", failing_dry_run)
        config = _config(tmp_path)

        with pytest.raises(OrchestratorError, match="pre-flight dry run failed"):
            run_session(session, scenario, version_a, version_b, 1, config, seed=1)

    def test_calibration_bridge_failure_raises_orchestrator_error(
        self, monkeypatch, tmp_path, session, scenario, app_versions
    ):
        """A `MetrionBridgeError` from settle/calibrate (e.g. a calibration timeout)
        must halt cleanly via `OrchestratorError`, not escape uncaught."""
        version_a, version_b = app_versions
        self._patch_all_success(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "metrilens.orchestrator.run_dry_run",
            lambda *a, **k: _step_log([("create_part", 1, 1)]),
        )

        def failing_calibration(*args, **kwargs):
            raise MetrionBridgeError("calibration log was not regenerated within 300s")

        monkeypatch.setattr("metrilens.orchestrator.force_idle_calibration", failing_calibration)
        config = _config(tmp_path)

        with pytest.raises(OrchestratorError, match="session settle/calibration failed"):
            run_session(session, scenario, version_a, version_b, 1, config, seed=1)

    @pytest.mark.parametrize(
        ("governors", "expected"),
        [(["conservative"], "conservative"), ([], None)],  # [] = host has no cpufreq at all.
    )
    def test_records_the_cpu_governor_it_read(
        self, monkeypatch, tmp_path, session, scenario, app_versions, governors, expected
    ):
        """The governor is *recorded*, not only asserted, and absent rather than
        guessed when the host has no cpufreq, so a session there is not credited with one.

        Patches the read so this asserts the wiring rather than restating it: comparing the
        row against a live call to the same functions would pass even if the orchestrator
        never wrote the column, because both sides would be reading the same machine.
        """
        version_a, version_b = app_versions
        self._patch_all_success(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "metrilens.orchestrator.run_dry_run",
            lambda *a, **k: _step_log([("create_part", 1, 1)]),
        )
        monkeypatch.setattr("metrilens.orchestrator.read_cpu_governors", lambda _root: governors)

        comparison_session = run_session(
            session,
            scenario,
            version_a,
            version_b,
            repetitions_per_version=1,
            config=_config(tmp_path),
            seed=1,
        )

        assert comparison_session.cpu_governor == expected

    def test_records_the_host_facts_it_collected(
        self, monkeypatch, tmp_path, session, scenario, app_versions
    ):
        """Patched, so this asserts the wiring and not the local machine: both that the row
        gets what `collect_host_facts` returns, and that it's called with the *configured*
        METRION binary rather than a hardcoded path, since the fingerprint has to describe the
        METRION this session was configured to run, which is the whole point of recording it."""
        version_a, version_b = app_versions
        self._patch_all_success(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "metrilens.orchestrator.run_dry_run",
            lambda *a, **k: _step_log([("create_part", 1, 1)]),
        )
        collected = {"kernel_release": "9.9.9-test", "metrion_package_sha256": "deadbeef"}
        seen = []
        monkeypatch.setattr(
            "metrilens.orchestrator.collect_host_facts",
            lambda binary: seen.append(binary) or dict(collected),
        )
        config = _config(tmp_path)

        comparison_session = run_session(
            session,
            scenario,
            version_a,
            version_b,
            repetitions_per_version=1,
            config=config,
            seed=1,
        )

        assert comparison_session.host_facts == collected
        assert seen == [config.metrion_binary_path]

    def test_passing_preflight_runs_full_session(
        self, monkeypatch, tmp_path, session, scenario, app_versions
    ):
        version_a, version_b = app_versions
        self._patch_all_success(monkeypatch, tmp_path)

        monkeypatch.setattr(
            "metrilens.orchestrator.run_dry_run",
            lambda *a, **k: _step_log([("create_part", 1, 1)]),
        )
        config = _config(tmp_path)

        comparison_session = run_session(
            session,
            scenario,
            version_a,
            version_b,
            repetitions_per_version=2,
            config=config,
            seed=1,
        )

        assert len(comparison_session.execution_order) == 4
        version_runs = (
            session.query(VersionRun).filter_by(comparison_session_id=comparison_session.id).all()
        )
        assert len(version_runs) == 2
        for version_run in version_runs:
            assert version_run.repetition_count_planned == 2
            assert len(version_run.repetitions) == 2
            assert all(r.status is RepetitionStatus.OK for r in version_run.repetitions)
            assert version_run.started_at is not None
            assert version_run.ended_at is not None

    def test_pass_overridden_verdict_also_proceeds(
        self, monkeypatch, tmp_path, session, scenario, app_versions
    ):
        version_a, version_b = app_versions
        self._patch_all_success(monkeypatch, tmp_path)
        log = _step_log([("create_part", 1, 1)])
        monkeypatch.setattr("metrilens.orchestrator.run_dry_run", lambda *a, **k: log)

        original_check_equivalence = check_equivalence

        def flag_then_override(session_, scenario_id, version_a_id, version_b_id, log_a, log_b):
            check = original_check_equivalence(
                session_, scenario_id, version_a_id, version_b_id, log_a, log_b
            )
            if check.verdict is EquivalenceVerdict.FLAGGED:
                check.override(notes="manually reviewed, acceptable")
                session_.add(check)
                session_.commit()
            return check

        monkeypatch.setattr("metrilens.orchestrator.check_equivalence", flag_then_override)
        config = _config(tmp_path)

        comparison_session = run_session(
            session,
            scenario,
            version_a,
            version_b,
            repetitions_per_version=1,
            config=config,
            seed=1,
        )
        assert comparison_session.id is not None
