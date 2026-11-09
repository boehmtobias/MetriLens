from pathlib import Path
from types import SimpleNamespace

import pytest
import smoke_test_session
from smoke_test_session import (
    check_passwordless_sudo,
    check_root_owned_copies,
    main,
)

from metrilens.db import get_engine, get_session
from metrilens.metrion_integration import _metrion_interpreter_pattern
from metrilens.models import AppVersion, RepetitionStatus, Scenario
from metrilens.orchestrator import OrchestratorError


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("metrion_binary_path: /x\n")
    return tmp_path


def _seed_scenario(name="Test Scenario") -> int:
    with get_session(get_engine()) as session:
        scenario = Scenario(name=name, steps=["create_part"])
        session.add(scenario)
        session.commit()
        return scenario.id


def _seed_versions(scenario_id: int, labels=("InvenTree DEBUG=True", "InvenTree DEBUG=False")):
    """The two rows `metrilens app-version load` would have inserted."""
    with get_session(get_engine()) as session:
        versions = [
            AppVersion(
                scenario_id=scenario_id,
                label=label,
                script_path=f"{label}.py",
                reset_script=f"reset_{label}.sh",
                compose_file=f"{label}/docker-compose.yml",
                health_url=f"http://localhost:8{index}80/api/",
            )
            for index, label in enumerate(labels)
        ]
        session.add_all(versions)
        session.commit()
        return [version.id for version in versions]


def _record_reaching_the_pipeline(monkeypatch) -> list:
    """Records whether the run got as far as reading the config, i.e. past version resolution."""
    reached: list = []
    monkeypatch.setattr("smoke_test_session.load_session_config", lambda *a, **k: reached.append(1))
    return reached


def _mock_pipeline(monkeypatch, fake_session):
    monkeypatch.setattr(
        "smoke_test_session.load_session_config",
        lambda *a, **k: SimpleNamespace(metrion_binary_path=Path("/x/metrion")),
    )
    monkeypatch.setattr("smoke_test_session.check_passwordless_sudo", lambda *a, **k: [])
    monkeypatch.setattr("smoke_test_session.run_session", lambda *a, **k: fake_session)
    monkeypatch.setattr("smoke_test_session.find_metrion_pids", lambda *a, **k: [])


class TestSmokeTestSession:
    def test_version_ids_are_required(self, runner, env):
        result = runner.invoke(main, [])
        assert result.exit_code != 0
        assert "Missing option" in result.output

    def test_unknown_version_id_errors(self, runner, env, monkeypatch):
        _seed_scenario()
        reached = _record_reaching_the_pipeline(monkeypatch)

        result = runner.invoke(main, ["--version-a-id", "1", "--version-b-id", "2"])

        # Asserted on behaviour rather than the message: loguru binds `sys.stderr` at import,
        # before pytest installs its per-test capture, so neither capsys nor capfd sees it.
        assert result.exit_code == 1
        assert not reached

    def test_versions_from_different_scenarios_rejected(self, runner, env, monkeypatch):
        first_id = _seed_scenario()
        second_id = _seed_scenario(name="Another Flow")
        (version_a_id,) = _seed_versions(first_id, labels=("a",))
        (version_b_id,) = _seed_versions(second_id, labels=("b",))
        reached = _record_reaching_the_pipeline(monkeypatch)

        result = runner.invoke(
            main, ["--version-a-id", str(version_a_id), "--version-b-id", str(version_b_id)]
        )

        assert result.exit_code == 1
        assert not reached

    def test_reports_success_without_writing_any_app_version(self, runner, env, monkeypatch):
        scenario_id = _seed_scenario()
        version_a_id, version_b_id = _seed_versions(scenario_id)

        fake_repetition = SimpleNamespace(
            repetition_index=0,
            status=RepetitionStatus.OK,
            cpu_energy_j=12.3,
            runtime_s=10.0,
            validation_notes=None,
        )
        fake_version_run = SimpleNamespace(id=1, repetitions=[fake_repetition])
        _mock_pipeline(
            monkeypatch,
            SimpleNamespace(id=99, execution_order=[1, 2], version_runs=[fake_version_run]),
        )

        result = runner.invoke(
            main, ["--version-a-id", str(version_a_id), "--version-b-id", str(version_b_id)]
        )

        assert result.exit_code == 0, result.output
        with get_session(get_engine()) as session:
            # The script reads rows the loader inserted; it must never add or change one.
            versions = session.query(AppVersion).all()
            assert len(versions) == 2
            assert {version.script_path for version in versions} == {
                "InvenTree DEBUG=True.py",
                "InvenTree DEBUG=False.py",
            }

    def test_orchestrator_error_exits_nonzero(self, runner, env, monkeypatch):
        scenario_id = _seed_scenario()
        version_a_id, version_b_id = _seed_versions(scenario_id)

        def fail(*a, **k):
            raise OrchestratorError("pre-flight equivalence_check 1 is FLAGGED")

        monkeypatch.setattr("smoke_test_session.load_session_config", lambda *a, **k: object())
        monkeypatch.setattr("smoke_test_session.check_passwordless_sudo", lambda *a, **k: [])
        monkeypatch.setattr("smoke_test_session.run_session", fail)

        result = runner.invoke(
            main, ["--version-a-id", str(version_a_id), "--version-b-id", str(version_b_id)]
        )

        assert result.exit_code == 1

    def test_sudo_problem_blocks_run_before_run_session(self, runner, env, monkeypatch):
        scenario_id = _seed_scenario()
        version_a_id, version_b_id = _seed_versions(scenario_id)

        called = []
        monkeypatch.setattr("smoke_test_session.load_session_config", lambda *a, **k: object())
        monkeypatch.setattr(
            "smoke_test_session.check_passwordless_sudo",
            lambda *a, **k: ["metrion binary (/opt/metrion)"],
        )
        monkeypatch.setattr("smoke_test_session.run_session", lambda *a, **k: called.append(1))

        result = runner.invoke(
            main, ["--version-a-id", str(version_a_id), "--version-b-id", str(version_b_id)]
        )

        assert result.exit_code == 1
        assert not called

    def test_failed_repetition_reports_nonzero(self, runner, env, monkeypatch):
        scenario_id = _seed_scenario()
        version_a_id, version_b_id = _seed_versions(scenario_id)

        fake_repetition = SimpleNamespace(
            repetition_index=0,
            status=RepetitionStatus.FAILED_MEASUREMENT,
            cpu_energy_j=None,
            runtime_s=None,
            validation_notes="lifecycle failure: boom",
        )
        fake_version_run = SimpleNamespace(id=1, repetitions=[fake_repetition])
        _mock_pipeline(
            monkeypatch,
            SimpleNamespace(id=1, execution_order=[1], version_runs=[fake_version_run]),
        )

        result = runner.invoke(
            main, ["--version-a-id", str(version_a_id), "--version-b-id", str(version_b_id)]
        )

        assert result.exit_code == 1


class TestCheckPasswordlessSudo:
    def test_all_permitted_returns_no_problems(self, tmp_path, monkeypatch):
        config = SimpleNamespace(
            metrion_binary_path=tmp_path / "metrion",
            metrion_idle_log_path=tmp_path / "rapl-idle.log",
            batch_cleanup_script_path=tmp_path / "cleanup_orphan_batch_files.py",
            analyze_wrapper_path=tmp_path / "metrion_analyze_wrapper.py",
            metrion_db_dir=tmp_path / "metrion-data",
            metrion_config_path=tmp_path / "metrion-config.yaml",
        )

        monkeypatch.setattr("smoke_test_session._sudo_permitted", lambda *cmd: True)

        assert check_passwordless_sudo(config) == []

    def test_reports_each_denied_check_by_label(self, tmp_path, monkeypatch):
        config = SimpleNamespace(
            metrion_binary_path=tmp_path / "metrion",
            metrion_idle_log_path=tmp_path / "rapl-idle.log",
            batch_cleanup_script_path=tmp_path / "cleanup_orphan_batch_files.py",
            analyze_wrapper_path=tmp_path / "metrion_analyze_wrapper.py",
            metrion_db_dir=tmp_path / "metrion-data",
            metrion_config_path=tmp_path / "metrion-config.yaml",
        )

        def fake_permitted(*command):
            return list(command) != ["rm", "-f", str(config.metrion_idle_log_path)]

        monkeypatch.setattr("smoke_test_session._sudo_permitted", fake_permitted)

        problems = check_passwordless_sudo(config)

        assert len(problems) == 1
        assert "idle log rm" in problems[0]
        assert str(config.metrion_idle_log_path) in problems[0]

    def test_it_checks_the_configured_cleanup_script_not_the_repo_default(self, tmp_path):
        """The grant names the configured copy, so checking the default would pass
        while every real cleanup call prompted for a password."""
        configured = tmp_path / "opt" / "cleanup_orphan_batch_files.py"
        configured_wrapper = tmp_path / "opt" / "metrion_analyze.py"
        config = SimpleNamespace(
            metrion_binary_path=tmp_path / "metrion",
            metrion_idle_log_path=tmp_path / "rapl-idle.log",
            batch_cleanup_script_path=configured,
            analyze_wrapper_path=configured_wrapper,
            metrion_db_dir=tmp_path / "metrion-data",
            metrion_config_path=tmp_path / "metrion-config.yaml",
        )
        checked = []

        def record(*command):
            checked.append(command)
            return True

        import smoke_test_session

        original = smoke_test_session._sudo_permitted
        smoke_test_session._sudo_permitted = record
        try:
            check_passwordless_sudo(config)
        finally:
            smoke_test_session._sudo_permitted = original

        assert any(str(configured) in command for command in checked)
        assert not any("scripts/cleanup_orphan_batch_files.py" in " ".join(c) for c in checked)


class TestCheckRootOwnedCopies:
    """Each root-owned copy must still match the checkout it came from."""

    def _setup(
        self, tmp_path, monkeypatch, *, installed_text="print('x')\n", source_text=None, owner_uid=0
    ):
        """Build a source/installed pair and report `owner_uid` for the installed file.

        The uid has to be faked: these tests run unprivileged, so a real `chown` to root is
        impossible, and without this every case would trip the ownership branch. Only the uid
        is substituted, and only for the installed path, so the real `chmod` modes the
        permission cases set are still what the check reads.
        """
        source = tmp_path / "checkout" / "cleanup_orphan_batch_files.py"
        source.parent.mkdir(parents=True)
        source.write_text(source_text if source_text is not None else installed_text)
        installed = tmp_path / "opt" / "bin" / "cleanup_orphan_batch_files.py"
        installed.parent.mkdir(parents=True)
        installed.write_text(installed_text)
        installed.chmod(0o755)
        installed.parent.chmod(0o755)
        monkeypatch.setattr(smoke_test_session, "DEFAULT_BATCH_CLEANUP_SCRIPT_PATH", source)
        # The analyze-wrapper pair is checked by the same function, so point it at its own
        # default to make it skip; these cases are about the cleanup-script pair.
        wrapper = tmp_path / "wrapper.py"
        wrapper.write_text("print('w')\n")
        monkeypatch.setattr(smoke_test_session, "DEFAULT_ANALYZE_WRAPPER_PATH", wrapper)

        real_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            result = real_stat(self, *args, **kwargs)
            if self == installed:
                return SimpleNamespace(st_uid=owner_uid, st_mode=result.st_mode)
            return result

        monkeypatch.setattr(Path, "stat", fake_stat)
        config = SimpleNamespace(batch_cleanup_script_path=installed, analyze_wrapper_path=wrapper)
        return config, source, installed

    def test_no_ops_when_no_separate_copy_is_configured(self, tmp_path, monkeypatch):
        # The configured path *is* the in-checkout default.
        source = tmp_path / "cleanup_orphan_batch_files.py"
        source.write_text("print('x')\n")
        monkeypatch.setattr(smoke_test_session, "DEFAULT_BATCH_CLEANUP_SCRIPT_PATH", source)
        wrapper = tmp_path / "metrion_analyze_wrapper.py"
        wrapper.write_text("print('w')\n")
        monkeypatch.setattr(smoke_test_session, "DEFAULT_ANALYZE_WRAPPER_PATH", wrapper)
        config = SimpleNamespace(batch_cleanup_script_path=source, analyze_wrapper_path=wrapper)
        assert check_root_owned_copies(config) == []

    def test_identical_root_owned_copy_passes(self, tmp_path, monkeypatch):
        config, _, _ = self._setup(tmp_path, monkeypatch)
        assert check_root_owned_copies(config) == []

    def test_stale_copy_is_reported(self, tmp_path, monkeypatch):
        config, source, installed = self._setup(
            tmp_path, monkeypatch, installed_text="old\n", source_text="new\n"
        )
        problems = check_root_owned_copies(config)
        assert len(problems) == 1
        assert "stale" in problems[0] and "batch cleanup script" in problems[0]
        assert str(installed) in problems[0] and str(source) in problems[0]

    def test_group_writable_copy_is_reported(self, tmp_path, monkeypatch):
        config, _, installed = self._setup(tmp_path, monkeypatch)
        installed.chmod(0o775)
        problems = check_root_owned_copies(config)
        assert any("group- or other-writable" in p and str(installed) in p for p in problems)

    def test_writable_parent_directory_is_reported(self, tmp_path, monkeypatch):
        # The gap a file-mode check alone misses: write access to the directory replaces the
        # file whatever the file's own mode says.
        config, _, installed = self._setup(tmp_path, monkeypatch)
        installed.parent.chmod(0o777)
        problems = check_root_owned_copies(config)
        assert any("its directory" in p and str(installed.parent) in p for p in problems)

    def test_non_root_owner_is_reported(self, tmp_path, monkeypatch):
        config, _, _ = self._setup(tmp_path, monkeypatch, owner_uid=1000)
        problems = check_root_owned_copies(config)
        assert any("not owned by root" in p for p in problems)

    def test_missing_source_is_reported_rather_than_passing_silently(self, tmp_path, monkeypatch):
        config, source, _ = self._setup(tmp_path, monkeypatch)
        source.unlink()
        problems = check_root_owned_copies(config)
        assert len(problems) == 1
        assert "cannot verify" in problems[0]

    def test_a_stale_and_insecure_copy_reports_both(self, tmp_path, monkeypatch):
        # The problems are independent, so a caller sees everything wrong at once rather than
        # fixing one, re-running, and discovering the next.
        config, _, installed = self._setup(
            tmp_path, monkeypatch, installed_text="old\n", source_text="new\n", owner_uid=1000
        )
        installed.chmod(0o775)
        problems = check_root_owned_copies(config)
        assert len(problems) == 3


class TestChecksMirrorTheRealCallSites:
    """Guards against false sudo denials in the pre-flight check.

    Every entry in `check_passwordless_sudo` must be the argv a call site actually builds. The
    grants are exact, so a stand-in that no real call would produce is not a harmless placeholder,
    it is a check that reports a denial for a permitted command. Conversely, a check looser than
    the grant passes while every real call prompts.
    """

    def _captured_commands(self, tmp_path, monkeypatch):
        config = SimpleNamespace(
            metrion_binary_path=tmp_path / "opt" / "bin" / "metrion",
            metrion_config_path=tmp_path / "opt" / "config.yaml",
            metrion_idle_log_path=tmp_path / "rapl-idle.log",
            batch_cleanup_script_path=tmp_path / "opt" / "cleanup.py",
            analyze_wrapper_path=tmp_path / "opt" / "metrion_analyze.py",
            metrion_db_dir=tmp_path / "metrion-data",
        )
        seen = []
        monkeypatch.setattr(
            "smoke_test_session._sudo_permitted", lambda *cmd: (seen.append(list(cmd)), True)[1]
        )
        check_passwordless_sudo(config)
        return config, seen

    def test_monitor_is_checked_by_full_argv_not_the_bare_binary(self, tmp_path, monkeypatch):
        # The grant names `monitor -c <config>`, so a bare-binary check matches nothing at all.
        config, seen = self._captured_commands(tmp_path, monkeypatch)
        assert [str(config.metrion_binary_path)] not in seen
        assert [
            str(config.metrion_binary_path),
            "monitor",
            "-c",
            str(config.metrion_config_path),
        ] in seen

    def test_both_signals_are_checked_on_the_pattern_the_signal_path_uses(
        self, tmp_path, monkeypatch
    ):
        """Checking one pattern while `_signal_metrion_tree` signals on another would let the
        pre-flight pass while the grants the stop path needs are absent from the policy."""
        config, seen = self._captured_commands(tmp_path, monkeypatch)
        pattern = _metrion_interpreter_pattern(config.metrion_binary_path)
        for command in (
            ["pkill", "-INT", "-f", pattern],
            ["pkill", "-KILL", "-f", pattern],
        ):
            assert command in seen, command

    def test_no_signal_command_carries_an_argument_containing_spaces(self, tmp_path, monkeypatch):
        """sudo-rs matches argument tokens rather than the joined argument string, so a grant whose
        arguments must be matched exactly cannot contain spaces: such a policy parses under
        `visudo -c` and is then refused anyway. This binds the signal commands only. The analyze
        wrapper legitimately takes timestamps with spaces in them, because its grant ends in a
        wildcard, which absorbs the rest of the argv whitespace and all.
        """
        _, seen = self._captured_commands(tmp_path, monkeypatch)
        for command in seen:
            if command[0] in ("pkill", "pgrep"):
                assert not any(" " in arg for arg in command), command

    def test_pgrep_is_not_checked_because_no_call_site_needs_root_for_it(
        self, tmp_path, monkeypatch
    ):
        """A check that passes for a command nothing runs can make missing grants look covered.
        `find_metrion_pids` reads `/proc` with a plain `pgrep`, which needs no grant, so none
        should be checked or granted."""
        _, seen = self._captured_commands(tmp_path, monkeypatch)
        assert not any(command[0] == "pgrep" for command in seen)

    def test_no_check_carries_a_placeholder_no_real_call_would_build(self, tmp_path, monkeypatch):
        _, seen = self._captured_commands(tmp_path, monkeypatch)
        flat = [arg for command in seen for arg in command]
        assert not any("preflight" in arg for arg in flat)

    def test_the_child_sweep_grants_are_covered(self, tmp_path, monkeypatch):
        # `_signal_metrion_tree` sends both signals this way on every stop.
        _, seen = self._captured_commands(tmp_path, monkeypatch)
        for signal in ("-INT", "-KILL"):
            assert any(c[:3] == ["pkill", signal, "-P"] for c in seen), signal
