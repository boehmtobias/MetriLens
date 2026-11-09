import io
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
from conftest import completed_process as _completed

from metrilens import metrion_integration
from metrilens.metrion_integration import (
    DEFAULT_MONITOR_STOP_TIMEOUT_S,
    MetrionBridgeError,
    _metrion_interpreter_pattern,
    confirm_metrion_bridge,
    find_metrion_pids,
    find_new_db_file,
    force_idle_calibration,
    load_metrion_binary_path,
    load_metrion_config_path,
    load_metrion_db_dir,
    load_metrion_idle_log_path,
    load_metrion_output_dir,
    monitor_command,
    read_monitor_log_tail,
    resolve_compose_container_map,
    resolve_compose_pids,
    run_analyze,
    snapshot_batch_files,
    start_monitor,
    stop_monitor,
)


def _fake_binary(tmp_path, exit_code: int = 0, stdout: str = "help text") -> str:
    binary = tmp_path / "fake-metrion"
    binary.write_text(f"#!/bin/sh\necho '{stdout}'\nexit {exit_code}\n")
    binary.chmod(0o755)
    return str(binary)


class TestLoadMetrionBinaryPath:
    def test_missing_key_raises(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("max_retries: 3\n")
        with pytest.raises(MetrionBridgeError, match="metrion_binary_path"):
            load_metrion_binary_path(config_path)

    def test_nonexistent_binary_raises(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_binary_path: {tmp_path / 'does-not-exist'}\n")
        with pytest.raises(MetrionBridgeError, match="not found"):
            load_metrion_binary_path(config_path)

    def test_valid_binary_resolves(self, tmp_path):
        binary_path = _fake_binary(tmp_path)
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_binary_path: {binary_path}\n")
        assert str(load_metrion_binary_path(config_path)) == binary_path

    def test_expands_user_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        binary_path = _fake_binary(tmp_path)
        relative = "~/" + str(binary_path)[len(str(tmp_path)) + 1 :]
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_binary_path: {relative}\n")
        assert str(load_metrion_binary_path(config_path)) == binary_path


class TestConfirmMetrionBridge:
    def test_successful_call_returns_stdout(self, tmp_path):
        binary_path = _fake_binary(tmp_path, exit_code=0, stdout="Usage: metrion")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_binary_path: {binary_path}\n")

        output = confirm_metrion_bridge(config_path)
        assert "Usage: metrion" in output

    def test_nonzero_exit_raises(self, tmp_path):
        binary_path = _fake_binary(tmp_path, exit_code=1, stdout="")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_binary_path: {binary_path}\n")

        with pytest.raises(MetrionBridgeError, match="exited 1"):
            confirm_metrion_bridge(config_path)


class TestLoadMetrionOutputDir:
    def test_missing_key_raises(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("max_retries: 3\n")
        with pytest.raises(MetrionBridgeError, match="metrion_output_dir"):
            load_metrion_output_dir(config_path)

    def test_nonexistent_dir_raises(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_output_dir: {tmp_path / 'nope'}\n")
        with pytest.raises(MetrionBridgeError, match="not found"):
            load_metrion_output_dir(config_path)

    def test_valid_dir_resolves(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_output_dir: {output_dir}\n")
        assert load_metrion_output_dir(config_path) == output_dir


class TestLoadMetrionIdleLogPath:
    def test_missing_key_raises(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("max_retries: 3\n")
        with pytest.raises(MetrionBridgeError, match="metrion_idle_log_path"):
            load_metrion_idle_log_path(config_path)

    def test_need_not_exist_yet(self, tmp_path):
        log_path = tmp_path / "log" / "rapl-idle.log"
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_idle_log_path: {log_path}\n")
        assert load_metrion_idle_log_path(config_path) == log_path


class TestLoadMetrionConfigPath:
    def test_missing_key_raises(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("max_retries: 3\n")
        with pytest.raises(MetrionBridgeError, match="metrion_config_path"):
            load_metrion_config_path(config_path)

    def test_valid_file_resolves(self, tmp_path):
        metrion_config = tmp_path / "metrion-config.yaml"
        metrion_config.write_text("general: {}\n")
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_config_path: {metrion_config}\n")
        assert load_metrion_config_path(config_path) == metrion_config


class TestLoadMetrionDbDir:
    def test_missing_key_raises(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("max_retries: 3\n")
        with pytest.raises(MetrionBridgeError, match="metrion_db_dir"):
            load_metrion_db_dir(config_path)

    def test_valid_dir_resolves(self, tmp_path):
        db_dir = tmp_path / "metrion"
        db_dir.mkdir()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"metrion_db_dir: {db_dir}\n")
        assert load_metrion_db_dir(config_path) == db_dir


class TestStopMonitorTimeout:
    """The grace period for METRION's own shutdown before the monitor is killed."""

    def test_default_grace_period_is_the_widened_one(self):
        import inspect

        signature = inspect.signature(metrion_integration.stop_monitor)
        assert signature.parameters["timeout_s"].default == DEFAULT_MONITOR_STOP_TIMEOUT_S
        # METRION's shutdown can stall indefinitely on some hosts, so a longer wait buys nothing.
        # Pinned to a literal so the default cannot be raised quietly; a host config may still
        # override it.
        assert DEFAULT_MONITOR_STOP_TIMEOUT_S == 90.0

    def test_the_kill_path_only_waits_long_enough_to_reap(self, monkeypatch):
        """A SIGKILLed process is gone at once, so the second wait must not be another full
        grace period; otherwise a genuinely stuck monitor costs twice the configured budget."""
        waits = []

        class _Process:
            args = ["sudo", "metrion", "monitor"]
            pid = 4242
            returncode = 0
            stderr = None

            def wait(self, timeout=None):
                waits.append(timeout)
                if len(waits) == 1:
                    raise subprocess.TimeoutExpired(cmd="metrion", timeout=timeout)

        monkeypatch.setattr(metrion_integration, "_signal_metrion_tree", lambda *a, **k: None)
        monkeypatch.setattr(
            metrion_integration, "_kill_orphaned_metrion_workers", lambda *a, **k: None
        )

        with pytest.raises(MetrionBridgeError, match="did not exit"):
            metrion_integration.stop_monitor(
                _Process(), Path("/nonexistent/metrion"), timeout_s=180
            )

        assert waits == [180, metrion_integration._KILL_REAP_TIMEOUT_S]


class TestFindNewDbFile:
    def test_prefers_newly_appeared_file(self, tmp_path):
        (tmp_path / "old.db").touch()
        before = set(tmp_path.glob("*.db"))
        started_at = time.time()
        new_file = tmp_path / "monitor_2026_01_01_00_00_00.db"
        new_file.touch()
        assert find_new_db_file(tmp_path, before, not_before=started_at) == new_file

    def test_falls_back_to_existing_file_when_none_appeared(self, tmp_path):
        """METRION's non-debug mode overwrites one fixed filename instead of adding a new one, so
        nothing appears in the diff and the file has to be recognised by its mtime."""
        fixed_path = tmp_path / "monitor.db"
        fixed_path.write_text("v1")
        before = set(tmp_path.glob("*.db"))
        started_at = time.time()
        fixed_path.write_text("v2 overwritten in place")
        assert find_new_db_file(tmp_path, before, not_before=started_at) == fixed_path

    def test_no_db_files_raises(self, tmp_path):
        with pytest.raises(MetrionBridgeError, match="no .db file in"):
            find_new_db_file(tmp_path, set(), not_before=time.time())

    def test_a_stale_database_is_never_returned(self, tmp_path):
        """If METRION's own `general.database.path` and MetriLens's `metrion_db_dir` point at
        different directories, nothing new appears where MetriLens looks. Returning the newest
        existing file would analyze a database from an earlier session and report plausible
        numbers from it, so this fails loudly and names the likely cause."""
        stale = tmp_path / "monitor_2026_08_11_05_45_51.db"
        stale.touch()
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        before = set(tmp_path.glob("*.db"))

        with pytest.raises(MetrionBridgeError, match="metrion_db_dir"):
            find_new_db_file(tmp_path, before, not_before=time.time())

    def test_a_coarse_filesystem_timestamp_still_counts_as_this_run(self, tmp_path):
        """Some filesystems store mtimes coarser than the clock the cutoff is read from, so a
        database written just after the monitor started can record an mtime just below it."""
        fixed_path = tmp_path / "monitor.db"
        fixed_path.touch()
        before = set(tmp_path.glob("*.db"))
        started_at = fixed_path.stat().st_mtime + 0.5

        assert find_new_db_file(tmp_path, before, not_before=started_at) == fixed_path


_TOP_HEADER = "UID  PID  PPID  C  STIME  TTY  TIME  CMD"


class TestResolveComposePids:
    def test_returns_every_pid_from_docker_top(self, monkeypatch, tmp_path):
        def fake_run(command, **kwargs):
            if "ps" in command:
                return _completed(stdout="container-a\ncontainer-b\n")
            top_output = {
                "container-a": f"{_TOP_HEADER}\nroot 111 1 0 18:16 ? 00:00:00 gunicorn\n",
                "container-b": (
                    f"{_TOP_HEADER}\n"
                    "root 222 1 0 18:16 ? 00:00:00 caddy\n"
                    "root 223 222 0 18:16 ? 00:00:00 caddy-worker\n"
                ),
            }[command[-1]]
            return _completed(stdout=top_output)

        monkeypatch.setattr("metrilens.metrion_integration.run", fake_run)
        pids = resolve_compose_pids(tmp_path / "docker-compose.yml")
        assert pids == [111, 222, 223]

    def test_ps_failure_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(returncode=1, stderr="ps failed"),
        )
        with pytest.raises(MetrionBridgeError, match="ps failed"):
            resolve_compose_pids(tmp_path / "docker-compose.yml")

    def test_no_containers_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed(stdout="")
        )
        with pytest.raises(MetrionBridgeError, match="no running containers"):
            resolve_compose_pids(tmp_path / "docker-compose.yml")

    def test_top_failure_raises(self, monkeypatch, tmp_path):
        def fake_run(command, **kwargs):
            if "ps" in command:
                return _completed(stdout="container-a\n")
            return _completed(returncode=1, stderr="top failed")

        monkeypatch.setattr("metrilens.metrion_integration.run", fake_run)
        with pytest.raises(MetrionBridgeError, match="top failed"):
            resolve_compose_pids(tmp_path / "docker-compose.yml")


class TestResolveComposeContainerMap:
    def test_maps_short_id_to_service_name(self, monkeypatch, tmp_path):
        ps_output = (
            '{"ID": "a7b5661bfd86", "Name": "inventree-cache", "Service": "inventree-cache"}\n'
            '{"ID": "6d9732cdeb23", "Name": "inventree-server", "Service": "inventree-server"}\n'
        )
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(stdout=ps_output),
        )
        container_map = resolve_compose_container_map(tmp_path / "docker-compose.yml")
        assert container_map == {
            "a7b5661bfd86": "inventree-cache",
            "6d9732cdeb23": "inventree-server",
        }

    def test_ps_failure_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(returncode=1, stderr="ps failed"),
        )
        with pytest.raises(MetrionBridgeError, match="ps failed"):
            resolve_compose_container_map(tmp_path / "docker-compose.yml")

    def test_no_containers_returns_empty_map(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed(stdout="")
        )
        assert resolve_compose_container_map(tmp_path / "docker-compose.yml") == {}


class TestFindMetrionPids:
    def test_returns_matching_pids(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(stdout="123\n456\n"),
        )
        assert find_metrion_pids(tmp_path / "venv" / "bin" / "metrion") == [123, 456]

    def test_returns_empty_list_when_none_running(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed(stdout="")
        )
        assert find_metrion_pids(tmp_path / "venv" / "bin" / "metrion") == []

    def test_matches_on_the_binarys_own_venv_interpreter_no_sudo(self, monkeypatch, tmp_path):
        """No `sudo`: reading another process's command line via `/proc` doesn't
        need root, and matching by interpreter path (not a
        generic "multiprocessing" substring) avoids ever touching another
        tenant's unrelated process on this shared machine."""
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return _completed(stdout="")

        monkeypatch.setattr("metrilens.metrion_integration.run", fake_run)
        binary_path = tmp_path / "venv" / "bin" / "metrion"
        find_metrion_pids(binary_path)

        assert captured["command"] == ["pgrep", "-f", str(tmp_path / "venv" / "bin" / "python3")]


class TestStartMonitor:
    def test_launches_via_sudo_with_config(self, monkeypatch, tmp_path):
        captured = {}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            return "fake-process"

        monkeypatch.setattr("metrilens.metrion_integration.Popen", fake_popen)
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed(stdout="")
        )
        binary_path = tmp_path / "metrion"
        config_path = tmp_path / "config.yaml"
        log_path = tmp_path / "run" / "metrion_monitor.log"
        result = start_monitor(binary_path, config_path, log_path)

        assert captured["command"] == ["sudo", str(binary_path), "monitor", "-c", str(config_path)]
        assert result == "fake-process"
        assert log_path.exists(), "the log file is created before the child needs to write to it"

    def test_refuses_to_start_if_already_running(self, monkeypatch, tmp_path):
        """Silently stacking on top of an already-running (or orphaned) instance
        would leave two monitors running unnoticed."""
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(stdout="789\n"),
        )
        monkeypatch.setattr(
            "metrilens.metrion_integration.Popen",
            lambda *a, **k: pytest.fail("should not launch a second instance"),
        )

        with pytest.raises(MetrionBridgeError, match="789"):
            start_monitor(
                tmp_path / "metrion", tmp_path / "config.yaml", tmp_path / "metrion_monitor.log"
            )


class _StubStoppableProcess:
    """Stand-in for `start_monitor`'s `Popen` (never a real subprocess).

    `stop_monitor` stops the real process via a second `sudo pkill` call
    rather than signaling this object directly (neither a plain `send_signal`
    nor `os.killpg` on the tracked `sudo` process reaches the real `metrion`
    process), so these tests drive it purely
    through `wait()`/`returncode`, matching what `stop_monitor` actually touches.
    """

    def __init__(self, args, wait_results, stderr_text=""):
        self.args = args
        self.pid = 4242
        self.returncode = None
        self.stderr = io.StringIO(stderr_text)
        self._wait_results = list(wait_results)

    def wait(self, timeout=None):
        result = self._wait_results.pop(0)
        if isinstance(result, Exception):
            raise result
        self.returncode = result
        return result


def _fake_run_with_pgrep(calls, child_pids: str = "9001\n"):
    """Answers any `pgrep -f`, privileged or not. The signal path looks PIDs up
    with a plain `pgrep` (reading `/proc` needs no root), so a stub keyed on `sudo pgrep` would
    silently make every lookup come back empty and every signal assertion vacuous."""

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["pgrep", "-f"]:
            return _completed(stdout=child_pids)
        return _completed()

    return fake_run


class TestMonitorOutputNeverGoesToAnUnreadPipe:
    """`stdout=PIPE, stderr=PIPE` with nothing reading them blocks METRION once 64 KiB
    accumulates, which can happen inside the measured window rather than at stop.
    """

    def test_output_is_redirected_to_the_log_file_and_no_pipe_is_used(self, monkeypatch, tmp_path):
        captured = {}

        def fake_popen(command, **kwargs):
            captured.update(kwargs)
            return "fake-process"

        monkeypatch.setattr("metrilens.metrion_integration.Popen", fake_popen)
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed(stdout="")
        )
        log_path = tmp_path / "slot_001" / "metrion_monitor.log"

        start_monitor(tmp_path / "metrion", tmp_path / "config.yaml", log_path)

        assert captured["stdout"].name == str(log_path)
        assert captured["stderr"] is subprocess.STDOUT
        assert "PIPE" not in repr(captured.values())

    def test_the_log_tail_is_what_error_messages_quote(self, tmp_path):
        log_path = tmp_path / "metrion_monitor.log"
        log_path.write_text("".join(f"line {n}\n" for n in range(50)))

        tail = read_monitor_log_tail(log_path, lines=3)

        assert tail == "line 47\nline 48\nline 49"

    def test_a_missing_log_yields_an_empty_tail_rather_than_raising(self, tmp_path):
        assert read_monitor_log_tail(tmp_path / "absent.log") == ""
        assert read_monitor_log_tail(None) == ""


class TestCalibrationSurvivesAKilledMonitor:
    def test_a_killed_calibration_monitor_still_returns_the_completed_log(
        self, monkeypatch, tmp_path
    ):
        """METRION's shutdown can stall indefinitely on some hosts, in `DataSourceRunner ...
        queue to join`, whether SIGINT goes to the tree or to the main process alone. The
        calibration only needs the idle log, and this branch runs because that log is already
        complete, so aborting the session over the stop would discard a finished calibration.
        """
        idle_log_path = tmp_path / "rapl-idle.log"
        idle_log_path.write_text("Package Total      :  10.0000 W\n")
        monkeypatch.setattr(
            "metrilens.metrion_integration.start_monitor",
            lambda binary_path, config_path, log_path: _StubMonitorProcess(),
        )

        def killed_stop(*args, **kwargs):
            raise metrion_integration.MonitorKilledError("did not exit within 60.0s, killed")

        monkeypatch.setattr("metrilens.metrion_integration.stop_monitor", killed_stop)
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed()
        )

        result = force_idle_calibration(
            tmp_path / "metrion",
            tmp_path / "config.yaml",
            idle_log_path,
            tmp_path / "metrion_monitor.log",
            poll_timeout_s=2,
            poll_interval_s=0.01,
        )

        assert result == idle_log_path

    def test_the_calibration_stop_waits_far_less_than_a_repetition_stop(self):
        """Nothing reads the calibration monitor's database, so waiting the repetition budget for a
        clean exit buys only tidiness."""
        calibration = metrion_integration.DEFAULT_CALIBRATION_STOP_TIMEOUT_S
        assert calibration < DEFAULT_MONITOR_STOP_TIMEOUT_S


class TestSudoSignalCall:
    """A refused `sudo` in the signal path must not look like a delivered signal, or a missing
    sudoers grant becomes a long wait and a timeout message that blames METRION for a signal it
    never received.
    """

    def test_a_refusing_sudo_raises_instead_of_reporting_nothing(self, monkeypatch):
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(
                returncode=1, stderr="sudo: interactive authentication is required"
            ),
        )
        with pytest.raises(MetrionBridgeError, match="nothing was signalled"):
            metrion_integration._signal_metrion_tree("-INT", Path("/opt/mr/venv/bin/metrion"))

    def test_no_match_is_not_a_failure(self, monkeypatch):
        """`pkill` exits 1 when its pattern matched nothing, which is the normal outcome of an
        orphan sweep that found no orphans, so the exit status cannot carry the error."""
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed(returncode=1)
        )
        metrion_integration._signal_metrion_tree("-KILL", Path("/opt/mr/venv/bin/metrion"))

    def test_pkill_killing_its_own_sudo_wrapper_is_not_a_failure(self, monkeypatch):
        """`sudo pkill -KILL -f <pattern>` matches its own wrapper, because that
        wrapper's command line contains the pattern being searched for. The targets die first and
        the call returns -9, so a returncode check would read a successful sweep as a broken one.
        """
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed(returncode=-9)
        )
        metrion_integration._signal_metrion_tree("-KILL", Path("/opt/mr/venv/bin/metrion"))


class TestMonitorArgvIsOneConstruction:
    def test_the_launched_argv_is_the_checked_argv(self):
        """The monitor grant names this argv exactly, so `start_monitor` and the pre-flight sudo
        check have to build it from the same place."""
        binary, config = Path("/opt/mr/venv/bin/metrion"), Path("/opt/mr/config.yaml")
        assert monitor_command(binary, config) == [
            str(binary),
            "monitor",
            "-c",
            str(config),
        ]

    def test_no_signal_grant_needs_an_argument_containing_spaces(self):
        """The shape sudo-rs forces. Every argument of every command in the signal
        path has to be a single token, or the grant cannot be written: sudo-rs matches argument
        tokens, so a pattern with spaces in it is unmatchable however it is spelled in sudoers.
        """
        binary = Path("/opt/mr/venv/bin/metrion")
        assert " " not in _metrion_interpreter_pattern(binary)


class TestStopMonitor:
    def test_graceful_stop_signals_children_then_parent(self, monkeypatch):
        process = _StubStoppableProcess(
            args=["sudo", "/opt/metrion", "monitor", "-c", "/opt/config.yaml"],
            wait_results=[0],
        )
        calls = []
        monkeypatch.setattr("metrilens.metrion_integration.run", _fake_run_with_pgrep(calls))

        stop_monitor(process, Path("/opt/metrion/venv/bin/metrion"), timeout_s=5)

        assert process.returncode == 0
        interpreter = "/opt/metrion/venv/bin/python3"
        assert calls == [
            # No `sudo` on the lookup: reading `/proc` needs none, so no such grant exists.
            ["pgrep", "-f", interpreter],
            ["sudo", "pkill", "-INT", "-P", "9001"],
            ["sudo", "pkill", "-INT", "-f", interpreter],
            # `_kill_orphaned_metrion_workers`, whose own lookup this stub also answers.
            ["pgrep", "-f", interpreter],
            ["sudo", "pkill", "-KILL", "-f", interpreter],
        ]

    def test_nonzero_exit_after_stop_raises(self, monkeypatch):
        process = _StubStoppableProcess(
            args=["sudo", "/opt/metrion", "monitor", "-c", "/opt/config.yaml"],
            wait_results=[2],
            stderr_text="crashed while stopping",
        )
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed()
        )

        with pytest.raises(MetrionBridgeError, match="exited 2"):
            stop_monitor(process, Path("/opt/metrion/venv/bin/metrion"), timeout_s=5)

    def test_still_running_after_int_gets_killed_including_children(self, monkeypatch):
        process = _StubStoppableProcess(
            args=["sudo", "/opt/metrion", "monitor", "-c", "/opt/config.yaml"],
            wait_results=[subprocess.TimeoutExpired(cmd="metrion", timeout=0.5), 0],
        )
        calls = []
        monkeypatch.setattr("metrilens.metrion_integration.run", _fake_run_with_pgrep(calls))

        with pytest.raises(MetrionBridgeError, match="killed"):
            stop_monitor(process, Path("/opt/metrion/venv/bin/metrion"), timeout_s=0.5)

        interpreter = "/opt/metrion/venv/bin/python3"
        assert calls[3:] == [
            ["pgrep", "-f", interpreter],
            ["sudo", "pkill", "-KILL", "-P", "9001"],
            ["sudo", "pkill", "-KILL", "-f", interpreter],
            ["pgrep", "-f", interpreter],
            ["sudo", "pkill", "-KILL", "-f", interpreter],
        ]

    def test_orphaned_worker_found_after_stop_gets_killed(self, monkeypatch):
        """A worker still matching after the main process exits is a
        genuine orphan (not a race with the process this call just stopped), so
        it gets killed via a second, independent sudo pkill."""
        process = _StubStoppableProcess(
            args=["sudo", "/opt/metrion", "monitor", "-c", "/opt/config.yaml"],
            wait_results=[0],
        )
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command == ["pgrep", "-f", "/opt/metrion/venv/bin/python3"]:
                return _completed(stdout="5555\n5556\n")
            return _completed()

        monkeypatch.setattr("metrilens.metrion_integration.run", fake_run)

        stop_monitor(process, Path("/opt/metrion/venv/bin/metrion"), timeout_s=5)

        assert calls[-2:] == [
            ["pgrep", "-f", "/opt/metrion/venv/bin/python3"],
            ["sudo", "pkill", "-KILL", "-f", "/opt/metrion/venv/bin/python3"],
        ]

    def test_batch_files_before_triggers_cleanup_even_without_orphans(self, monkeypatch, tmp_path):
        """Cleanup must run even when no orphaned PID is found, since a
        process can finish writing its files and exit on its own before this sweep."""
        monkeypatch.setattr(metrion_integration, "_BATCH_FILE_DIR", tmp_path)
        before = snapshot_batch_files()
        new_file = tmp_path / "batch_abc123.ipc"
        new_file.write_text("data")

        process = _StubStoppableProcess(
            args=["sudo", "/opt/metrion", "monitor", "-c", "/opt/config.yaml"],
            wait_results=[0],
        )
        calls = []
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: calls.append(command) or _completed(),
        )

        stop_monitor(
            process,
            Path("/opt/metrion/venv/bin/metrion"),
            timeout_s=5,
            batch_files_before=before,
        )

        cleanup_python = metrion_integration._SYSTEM_PYTHON
        cleanup_calls = [c for c in calls if c[0] == "sudo" and c[1] == cleanup_python]
        assert cleanup_calls == [
            [
                "sudo",
                metrion_integration._SYSTEM_PYTHON,
                str(_CLEANUP_SCRIPT),
                str(new_file),
            ]
        ]


class TestSnapshotBatchFiles:
    def test_returns_only_matching_files(self, monkeypatch, tmp_path):
        monkeypatch.setattr(metrion_integration, "_BATCH_FILE_DIR", tmp_path)
        (tmp_path / "batch_one.ipc").write_text("a")
        (tmp_path / "batch_two.ipc").write_text("b")
        (tmp_path / "unrelated.txt").write_text("c")

        result = snapshot_batch_files()

        assert result == {tmp_path / "batch_one.ipc", tmp_path / "batch_two.ipc"}


_CLEANUP_SCRIPT = metrion_integration.DEFAULT_BATCH_CLEANUP_SCRIPT_PATH


class TestCleanupNewBatchFiles:
    def test_deletes_only_files_that_appeared_since_snapshot(self, monkeypatch, tmp_path):
        monkeypatch.setattr(metrion_integration, "_BATCH_FILE_DIR", tmp_path)
        (tmp_path / "batch_preexisting.ipc").write_text("a")
        before = snapshot_batch_files()
        (tmp_path / "batch_new.ipc").write_text("b")

        calls = []
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: calls.append(command) or _completed(),
        )

        metrion_integration._cleanup_new_batch_files(before, _CLEANUP_SCRIPT)

        assert len(calls) == 1
        assert calls[0] == [
            "sudo",
            metrion_integration._SYSTEM_PYTHON,
            str(_CLEANUP_SCRIPT),
            str(tmp_path / "batch_new.ipc"),
        ]

    def test_no_new_files_skips_the_call_entirely(self, monkeypatch, tmp_path):
        monkeypatch.setattr(metrion_integration, "_BATCH_FILE_DIR", tmp_path)
        (tmp_path / "batch_preexisting.ipc").write_text("a")
        before = snapshot_batch_files()

        calls = []
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: calls.append(command) or _completed(),
        )

        metrion_integration._cleanup_new_batch_files(before, _CLEANUP_SCRIPT)

        assert calls == []

    def test_nonzero_exit_logs_warning_but_does_not_raise(self, monkeypatch, tmp_path):
        monkeypatch.setattr(metrion_integration, "_BATCH_FILE_DIR", tmp_path)
        before = snapshot_batch_files()
        (tmp_path / "batch_new.ipc").write_text("b")
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(returncode=1, stderr="permission denied"),
        )

        metrion_integration._cleanup_new_batch_files(before, _CLEANUP_SCRIPT)


class TestRunAnalyze:
    def test_runs_via_sudo(self, monkeypatch, tmp_path):
        """`metrion analyze` needs write access to the root-owned `.db` file
        `metrion monitor` produces (without `sudo` the open fails
        with a permission error that METRION itself only logs and swallows,
        still exiting 0 with nothing written)."""
        captured = {}
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        def fake_run(command, **kwargs):
            captured["command"] = command
            (output_dir / "energy_summary_123.csv").write_text("new")
            return _completed()

        monkeypatch.setattr("metrilens.metrion_integration.run", fake_run)
        wrapper_path = tmp_path / "metrion_analyze_wrapper.py"
        run_analyze(
            wrapper_path=wrapper_path,
            db_path=tmp_path / "run.db",
            start_time=datetime(2026, 7, 28, 10, 0, 0),
            end_time=datetime(2026, 7, 28, 10, 5, 0),
            metrion_output_dir=output_dir,
            archive_dir=tmp_path / "archive",
        )

        # The sudo'd command is the root-owned wrapper under the system
        # interpreter, not METRION itself: sudo-rs rejects wildcards in command arguments, so
        # the varying values could never have been granted inside METRION's own argv.
        assert captured["command"][:3] == [
            "sudo",
            metrion_integration._SYSTEM_PYTHON,
            str(wrapper_path),
        ]

    def test_a_silent_failure_quotes_metrions_own_output(self, monkeypatch, tmp_path):
        """METRION swallows several errors internally and still exits 0, so this branch is the
        only place the reason can come from."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(
                stderr="duckdb.IOException: could not open database in read-write mode"
            ),
        )

        with pytest.raises(MetrionBridgeError, match="read-write mode"):
            run_analyze(
                wrapper_path=tmp_path / "wrapper.py",
                db_path=tmp_path / "run.db",
                start_time=datetime(2026, 7, 28, 10, 0, 0),
                end_time=datetime(2026, 7, 28, 10, 5, 0),
                metrion_output_dir=output_dir,
                archive_dir=tmp_path / "archive",
            )

    def test_a_silent_failure_says_so_when_metrion_printed_nothing(self, monkeypatch, tmp_path):
        """No exception, no new export files, and nothing on stdout/stderr either: both halves
        of the failure message have to be present, since each names a distinct symptom a
        reader would otherwise have to diagnose by rerunning `metrion analyze` themselves."""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: _completed()
        )

        with pytest.raises(MetrionBridgeError, match="wrote no new files") as exc_info:
            run_analyze(
                wrapper_path=tmp_path / "wrapper.py",
                db_path=tmp_path / "run.db",
                start_time=datetime(2026, 7, 28, 10, 0, 0),
                end_time=datetime(2026, 7, 28, 10, 5, 0),
                metrion_output_dir=output_dir,
                archive_dir=tmp_path / "archive",
            )
        assert "wrote nothing to stdout or stderr" in str(exc_info.value)

    def test_passes_only_the_three_varying_values_and_no_pid_list(self, monkeypatch, tmp_path):
        """The fixed flags live in the wrapper, so only the varying values are passed.

        A PID list cannot be used at all, since it can only be snapshotted before the monitor
        starts and misses everything the stack forks afterwards. What reaches the wrapper is
        exactly the database path and the two window bounds, which is what its grant wildcards
        and what its own validation constrains. That the wrapper then passes
        `--filter-cmdline .` is asserted in `tests/test_metrion_analyze_wrapper.py` against
        `EXPECTED_WRAPPER_ANALYZE_ARGS`, so the property still has a test, one level down."""
        captured = {}
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        def fake_run(command, **kwargs):
            captured["command"] = command
            (output_dir / "energy_summary_123.csv").write_text("new")
            return _completed()

        monkeypatch.setattr("metrilens.metrion_integration.run", fake_run)
        run_analyze(
            wrapper_path=tmp_path / "metrion_analyze_wrapper.py",
            db_path=tmp_path / "run.db",
            start_time=datetime(2026, 7, 28, 10, 0, 0),
            end_time=datetime(2026, 7, 28, 10, 5, 0),
            metrion_output_dir=output_dir,
            archive_dir=tmp_path / "archive",
        )

        command = captured["command"]
        assert "--filter-pids" not in command
        assert command[3:] == [
            str(tmp_path / "run.db"),
            "2026-07-28 10:00:00",
            "2026-07-28 10:05:00",
        ]

    def test_success_archives_new_files_only(self, monkeypatch, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / "stale_from_earlier_repetition.csv").write_text("old")
        archive_dir = tmp_path / "archive"

        def fake_run(command, **kwargs):
            (output_dir / "energy_summary_123.csv").write_text("new")
            (output_dir / "energy_stats_123.json").write_text("{}")
            return _completed()

        monkeypatch.setattr("metrilens.metrion_integration.run", fake_run)
        archived = run_analyze(
            wrapper_path=tmp_path / "metrion_analyze_wrapper.py",
            db_path=tmp_path / "run.db",
            start_time=datetime(2026, 7, 28, 10, 0, 0),
            end_time=datetime(2026, 7, 28, 10, 5, 0),
            metrion_output_dir=output_dir,
            archive_dir=archive_dir,
        )

        assert set(archived) == {"energy_summary_123.csv", "energy_stats_123.json"}
        assert (archive_dir / "energy_summary_123.csv").read_text() == "new"
        assert not (output_dir / "energy_summary_123.csv").exists()
        assert (output_dir / "stale_from_earlier_repetition.csv").exists()

    def test_nonzero_exit_raises_without_archiving(self, monkeypatch, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(returncode=1, stderr="analyze failed"),
        )
        with pytest.raises(MetrionBridgeError, match="analyze failed"):
            run_analyze(
                wrapper_path=tmp_path / "metrion_analyze_wrapper.py",
                db_path=tmp_path / "run.db",
                start_time=datetime(2026, 7, 28, 10, 0, 0),
                end_time=datetime(2026, 7, 28, 10, 5, 0),
                metrion_output_dir=output_dir,
                archive_dir=tmp_path / "archive",
            )


class _StubMonitorProcess:
    """Stand-in for the `Popen` `start_monitor` would return (never a real subprocess).

    `force_idle_calibration` does not trust `metrion monitor --timeout` to exit on
    its own (it does not reliably do so), so
    these tests exercise its own launch/poll/stop loop rather than a blocking `run()`.
    """

    def __init__(self, returncode: int | None = None, stderr_text: str = "", args=None):
        self.killed = False
        self.returncode = returncode
        self.stderr = io.StringIO(stderr_text)
        self.args = args or ["sudo", "/opt/metrion", "monitor", "-c", "/opt/config.yaml"]

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class TestForceIdleCalibration:
    def test_deletes_via_sudo_then_polls_until_marker_present_then_stops(
        self, monkeypatch, tmp_path
    ):
        idle_log_path = tmp_path / "log" / "rapl-idle.log"
        idle_log_path.parent.mkdir()
        idle_log_path.write_text("stale calibration")
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            idle_log_path.unlink()
            return _completed()

        monkeypatch.setattr("metrilens.metrion_integration.run", fake_run)

        process = _StubMonitorProcess()

        def fake_start_monitor(binary_path, config_path, log_path):
            # No "Package Total" yet, mirroring METRION's own freezer step, which
            # can stall well past a short file-size-stability window before the real
            # measurement is appended.
            idle_log_path.write_text("Waiting for FreezerState=frozen...\n")
            return process

        monkeypatch.setattr("metrilens.metrion_integration.start_monitor", fake_start_monitor)
        stopped = []
        monkeypatch.setattr(
            "metrilens.metrion_integration.stop_monitor", lambda p, b, **k: stopped.append(p)
        )

        sleep_calls = 0
        original_sleep = time.sleep

        def fake_sleep(seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 2:
                idle_log_path.write_text("Package Total      :  10.0000 W\n")
            original_sleep(0)

        monkeypatch.setattr("metrilens.metrion_integration.time.sleep", fake_sleep)

        result = force_idle_calibration(
            tmp_path / "metrion",
            tmp_path / "config.yaml",
            idle_log_path,
            tmp_path / "metrion_monitor.log",
            poll_timeout_s=2,
            poll_interval_s=0.01,
        )

        assert result == idle_log_path
        assert "Package Total" in idle_log_path.read_text()
        assert calls[0] == ["sudo", "rm", "-f", str(idle_log_path)]
        assert stopped == [process]

    def test_passes_a_batch_file_snapshot_through_to_stop_monitor(self, monkeypatch, tmp_path):
        """The snapshot must be taken before `start_monitor` launches, so
        it's handed to `stop_monitor` on the happy path, not left as `None`."""
        idle_log_path = tmp_path / "rapl-idle.log"
        batch_dir = tmp_path / "tmp"
        batch_dir.mkdir()
        monkeypatch.setattr(metrion_integration, "_BATCH_FILE_DIR", batch_dir)
        (batch_dir / "batch_preexisting.ipc").write_text("a")

        def fake_start_monitor(binary_path, config_path, log_path):
            # A new file appears once "monitor" launches, after the snapshot is taken.
            (batch_dir / "batch_new.ipc").write_text("b")
            idle_log_path.write_text("Package Total      :  10.0000 W\n")
            return _StubMonitorProcess()

        monkeypatch.setattr("metrilens.metrion_integration.start_monitor", fake_start_monitor)
        received = {}
        monkeypatch.setattr(
            "metrilens.metrion_integration.stop_monitor",
            lambda p, b, **kwargs: received.update(kwargs),
        )

        force_idle_calibration(
            tmp_path / "metrion",
            tmp_path / "config.yaml",
            idle_log_path,
            tmp_path / "metrion_monitor.log",
            poll_timeout_s=2,
            poll_interval_s=0.01,
        )

        assert received["batch_files_before"] == {batch_dir / "batch_preexisting.ipc"}

    def test_skips_delete_when_log_does_not_exist_yet(self, monkeypatch, tmp_path):
        idle_log_path = tmp_path / "rapl-idle.log"
        calls = []
        monkeypatch.setattr(
            "metrilens.metrion_integration.run", lambda command, **kwargs: calls.append(command)
        )

        def fake_start_monitor(binary_path, config_path, log_path):
            idle_log_path.write_text("Package Total      :  10.0000 W\n")
            return _StubMonitorProcess()

        monkeypatch.setattr("metrilens.metrion_integration.start_monitor", fake_start_monitor)
        monkeypatch.setattr("metrilens.metrion_integration.stop_monitor", lambda p, b, **k: None)

        force_idle_calibration(
            tmp_path / "metrion",
            tmp_path / "config.yaml",
            idle_log_path,
            tmp_path / "metrion_monitor.log",
            poll_timeout_s=2,
            poll_interval_s=0.01,
        )

        assert calls == []

    def test_delete_permission_failure_raises(self, monkeypatch, tmp_path):
        idle_log_path = tmp_path / "rapl-idle.log"
        idle_log_path.write_text("root-owned stale calibration")
        monkeypatch.setattr(
            "metrilens.metrion_integration.run",
            lambda command, **kwargs: _completed(returncode=1, stderr="Permission denied"),
        )
        with pytest.raises(MetrionBridgeError, match="Permission denied"):
            force_idle_calibration(
                tmp_path / "metrion",
                tmp_path / "config.yaml",
                idle_log_path,
                tmp_path / "metrion_monitor.log",
            )

    def test_log_not_regenerated_kills_the_real_tree_not_just_the_sudo_wrapper(
        self, monkeypatch, tmp_path
    ):
        """A plain `process.kill()` only reaches the `sudo` wrapper, not the
        actual escalated `metrion` process, which would keep running and consuming
        CPU. Must use the same signal-tree + orphan-sweep cleanup `stop_monitor` uses."""
        idle_log_path = tmp_path / "rapl-idle.log"
        binary_path = tmp_path / "venv" / "bin" / "metrion"
        process = _StubMonitorProcess(
            args=["sudo", str(binary_path), "monitor", "-c", "/opt/config.yaml"]
        )
        monkeypatch.setattr(
            "metrilens.metrion_integration.start_monitor",
            lambda binary_path, config_path, log_path: process,
        )
        calls = []
        monkeypatch.setattr("metrilens.metrion_integration.run", _fake_run_with_pgrep(calls))

        with pytest.raises(MetrionBridgeError, match="did not complete"):
            force_idle_calibration(
                binary_path,
                tmp_path / "config.yaml",
                idle_log_path,
                tmp_path / "metrion_monitor.log",
                poll_timeout_s=0.05,
                poll_interval_s=0.01,
            )

        interpreter = str(tmp_path / "venv" / "bin" / "python3")
        assert calls == [
            ["pgrep", "-f", interpreter],
            ["sudo", "pkill", "-KILL", "-P", "9001"],
            ["sudo", "pkill", "-KILL", "-f", interpreter],
            ["pgrep", "-f", interpreter],
            ["sudo", "pkill", "-KILL", "-f", interpreter],
        ]
        assert process.killed is False

    def test_a_complete_log_is_accepted_even_after_the_deadline_expires(
        self, monkeypatch, tmp_path
    ):
        """METRION's calibration freezes `user.slice`, MetriLens runs under it, and
        `time.monotonic()` keeps advancing while a frozen process is not scheduled
        (a freeze of N seconds advances it N seconds). So the poll loop can fail to run at
        all and the deadline still expire, with the calibration finished and its marker
        unread. `poll_timeout_s=0` models the extreme: the loop body never executes."""
        idle_log_path = tmp_path / "rapl-idle.log"
        monkeypatch.setattr("metrilens.metrion_integration.run", lambda *a, **k: _completed())
        process = _StubMonitorProcess()

        def fake_start_monitor(binary_path, config_path, log_path):
            # Written by METRION while MetriLens is frozen and cannot observe it.
            idle_log_path.write_text("Package Total      :  19.0398 W\n")
            return process

        monkeypatch.setattr("metrilens.metrion_integration.start_monitor", fake_start_monitor)
        stopped = []
        monkeypatch.setattr(
            "metrilens.metrion_integration.stop_monitor", lambda p, b, **k: stopped.append(p)
        )
        killed = []
        monkeypatch.setattr(
            "metrilens.metrion_integration._signal_metrion_tree",
            lambda *a, **k: killed.append(a),
        )

        result = force_idle_calibration(
            tmp_path / "metrion",
            tmp_path / "config.yaml",
            idle_log_path,
            tmp_path / "metrion_monitor.log",
            poll_timeout_s=0,
            poll_interval_s=0.01,
        )

        assert result == idle_log_path
        # Accepted through the normal path: the monitor is stopped, not killed.
        assert stopped == [process]
        assert killed == []

    def test_failure_message_reports_what_was_observed(self, monkeypatch, tmp_path):
        """A failure must distinguish an absent log from a present-but-incomplete one,
        so the message does not point at METRION's measurement when the thaw is at fault."""
        idle_log_path = tmp_path / "rapl-idle.log"
        monitor_log_path = tmp_path / "metrion_monitor.log"
        monitor_log_path.write_text("some monitor output\n")
        monkeypatch.setattr("metrilens.metrion_integration.run", lambda *a, **k: _completed())
        monkeypatch.setattr(
            "metrilens.metrion_integration.start_monitor",
            lambda binary_path, config_path, log_path: _StubMonitorProcess(),
        )
        monkeypatch.setattr("metrilens.metrion_integration._signal_metrion_tree", lambda *a: None)
        monkeypatch.setattr(
            "metrilens.metrion_integration._kill_orphaned_metrion_workers", lambda *a: None
        )

        with pytest.raises(MetrionBridgeError, match="the log does not exist"):
            force_idle_calibration(
                tmp_path / "metrion",
                tmp_path / "config.yaml",
                idle_log_path,
                monitor_log_path,
                poll_timeout_s=0.02,
                poll_interval_s=0.01,
            )

        idle_log_path.write_text("Waiting for FreezerState=frozen...\n")
        with pytest.raises(MetrionBridgeError, match="holds no 'Package Total' line"):
            force_idle_calibration(
                tmp_path / "metrion",
                tmp_path / "config.yaml",
                idle_log_path,
                monitor_log_path,
                poll_timeout_s=0.02,
                poll_interval_s=0.01,
            )

    def test_early_process_exit_raises_immediately(self, monkeypatch, tmp_path):
        """A `sudo` that never authenticated looks exactly like a slow calibration until the poll
        timeout. The reason reaches the message through METRION's own log file."""
        idle_log_path = tmp_path / "rapl-idle.log"
        monitor_log_path = tmp_path / "metrion_monitor.log"
        monitor_log_path.write_text("sudo: 3 incorrect password attempts\n")
        process = _StubMonitorProcess(returncode=1)
        monkeypatch.setattr(
            "metrilens.metrion_integration.start_monitor",
            lambda binary_path, config_path, log_path: process,
        )

        with pytest.raises(MetrionBridgeError, match="incorrect password attempts"):
            force_idle_calibration(
                tmp_path / "metrion",
                tmp_path / "config.yaml",
                idle_log_path,
                monitor_log_path,
                poll_timeout_s=300,
                poll_interval_s=0.01,
            )
