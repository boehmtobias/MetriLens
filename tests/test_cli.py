from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from metrilens import recording
from metrilens.cli import cli
from metrilens.db import get_engine, get_session
from metrilens.evaluation import EvaluationError
from metrilens.models import (
    AppVersion,
    ComparisonSession,
    Repetition,
    RepetitionStatus,
    Scenario,
    VersionRun,
)
from metrilens.orchestrator import OrchestratorError


def _create_comparison_session(scenario_id):
    with get_session(get_engine()) as session:
        comparison_session = ComparisonSession(
            scenario_id=scenario_id,
            host_descriptor="x",
            metrion_config_path="c.yaml",
            idle_power_calibration={},
            calibration_timestamp=datetime.now(UTC),
            execution_order=[],
            random_seed=1,
        )
        session.add(comparison_session)
        session.commit()
        return comparison_session.id


@pytest.mark.parametrize("wrong_way", ["nonexistent_id", "scenario_id_mismatch"])
def test_report_missing_session_errors(runner, tmp_path, monkeypatch, wrong_way):
    """`report` refuses both a `comparison_session_id` that doesn't exist and one that exists
    but under a different `scenario_id` than the one given; the same guard clause either way."""
    monkeypatch.chdir(tmp_path)
    if wrong_way == "nonexistent_id":
        scenario_id, comparison_session_id = "1", "999"
    else:
        scenario_id, _version_a_id, _version_b_id = _seed_scenario_and_versions(tmp_path)
        comparison_session_id = _create_comparison_session(scenario_id)
        scenario_id = "999"  # the real session belongs to a different scenario id

    result = runner.invoke(
        cli,
        [
            "report",
            "--scenario-id",
            str(scenario_id),
            "--comparison-session-id",
            str(comparison_session_id),
        ],
    )
    assert result.exit_code == 1
    assert "no comparison_session" in result.output


def test_report_invokes_run_evaluation_and_prints_output_dir(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario_id, _version_a_id, _version_b_id = _seed_scenario_and_versions(tmp_path)
    with get_session(get_engine()) as session:
        comparison_session = ComparisonSession(
            scenario_id=scenario_id,
            host_descriptor="x",
            metrion_config_path="c.yaml",
            idle_power_calibration={},
            calibration_timestamp=datetime.now(UTC),
            execution_order=[],
            random_seed=1,
        )
        session.add(comparison_session)
        session.commit()
        comparison_session_id = comparison_session.id

    monkeypatch.setattr(
        "metrilens.cli.run_evaluation", lambda *a, **k: Path("evaluation/workstation1/1_1")
    )
    result = runner.invoke(
        cli,
        [
            "report",
            "--scenario-id",
            str(scenario_id),
            "--comparison-session-id",
            str(comparison_session_id),
        ],
    )
    assert result.exit_code == 0
    assert "evaluation/workstation1/1_1" in result.output


def test_report_evaluation_error_exits_nonzero(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario_id, _version_a_id, _version_b_id = _seed_scenario_and_versions(tmp_path)
    with get_session(get_engine()) as session:
        comparison_session = ComparisonSession(
            scenario_id=scenario_id,
            host_descriptor="x",
            metrion_config_path="c.yaml",
            idle_power_calibration={},
            calibration_timestamp=datetime.now(UTC),
            execution_order=[],
            random_seed=1,
        )
        session.add(comparison_session)
        session.commit()
        comparison_session_id = comparison_session.id

    def fake_run_evaluation(*a, **k):
        raise EvaluationError("no equivalence_check found for comparison_session 1")

    monkeypatch.setattr("metrilens.cli.run_evaluation", fake_run_evaluation)
    result = runner.invoke(
        cli,
        [
            "report",
            "--scenario-id",
            str(scenario_id),
            "--comparison-session-id",
            str(comparison_session_id),
        ],
    )
    assert result.exit_code == 1
    assert "no equivalence_check" in result.output


def test_scenario_load_rejects_missing_steps(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text("name: test\n")
    result = runner.invoke(cli, ["scenario", "load", str(scenario_file)])
    assert result.exit_code == 1
    assert "steps" in result.output


def test_scenario_load_inserts_scenario_row(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text(
        "name: InvenTree Core Flow\ndescription: a flow\nsteps: [create_part, adjust_stock]\n"
    )

    result = runner.invoke(cli, ["scenario", "load", str(scenario_file)])
    assert result.exit_code == 0
    assert "InvenTree Core Flow" in result.output
    assert "2 steps" in result.output

    from metrilens.db import get_engine, get_session
    from metrilens.models import Scenario

    with get_session(get_engine()) as session:
        rows = session.query(Scenario).all()
        assert len(rows) == 1
        assert rows[0].name == "InvenTree Core Flow"
        assert rows[0].steps == ["create_part", "adjust_stock"]
        assert rows[0].iterations == 1


def test_scenario_load_stores_declared_iterations(runner, tmp_path, monkeypatch):
    """k reaches the row, since the report cannot state it otherwise."""
    monkeypatch.chdir(tmp_path)
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text(
        "name: Session Flow\nsteps: [create_part, browse_catalog]\niterations: 3\n"
    )

    result = runner.invoke(cli, ["scenario", "load", str(scenario_file)])
    assert result.exit_code == 0
    assert "3 iteration(s)" in result.output

    from metrilens.db import get_engine, get_session
    from metrilens.models import Scenario

    with get_session(get_engine()) as session:
        assert session.query(Scenario).one().iterations == 3


def test_scenario_load_rejects_an_unknown_key(runner, tmp_path, monkeypatch):
    """A typo in `iterations` would otherwise load a k=3 scenario as a k=1 row, run three
    iterations anyway, and report a number nothing says how to interpret."""
    monkeypatch.chdir(tmp_path)
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text("name: Session Flow\nsteps: [create_part]\niteration: 3\n")

    result = runner.invoke(cli, ["scenario", "load", str(scenario_file)])
    assert result.exit_code == 1
    assert "iteration" in result.output


@pytest.mark.parametrize("value", ["0", "'three'", "true"])
def test_scenario_load_rejects_a_nonsense_iteration_count(runner, tmp_path, monkeypatch, value):
    """Three distinct halves of the guard: `0` is the right type but out of range, `'three'` is
    the wrong type entirely, and `true` needs its own case since YAML's bool is a Python `int`
    subclass, and a generic `not isinstance(x, int)` check would silently accept it."""
    monkeypatch.chdir(tmp_path)
    scenario_file = tmp_path / "scenario.yaml"
    scenario_file.write_text(f"name: S\nsteps: [a]\niterations: {value}\n")

    result = runner.invoke(cli, ["scenario", "load", str(scenario_file)])
    assert result.exit_code == 1
    assert "iterations" in result.output


_VERSIONS_YAML = """versions:
  - label: InvenTree DEBUG=True
    description: debug on
    script_path: /abs/debug_true.py
    reset_script: /abs/reset_debug_true.sh
    compose_file: /abs/debug-true/docker-compose.yml
    health_url: http://localhost:8280/api/
    version_ref: DEBUG=True
  - label: InvenTree DEBUG=False
    script_path: /abs/debug_false.py
    reset_script: /abs/reset_debug_false.sh
    compose_file: /abs/debug-false/docker-compose.yml
    health_url: http://localhost:8180/api/
"""


def _write_versions_yaml(tmp_path, content=_VERSIONS_YAML):
    versions_file = tmp_path / "app_versions.yaml"
    versions_file.write_text(content)
    return versions_file


def _seed_bare_scenario():
    with get_session(get_engine()) as session:
        scenario = Scenario(name="s", steps=["create_part"])
        session.add(scenario)
        session.commit()
        return scenario.id


def test_app_version_load_rejects_missing_versions_key(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    versions_file = _write_versions_yaml(tmp_path, "name: not an app version file\n")
    result = runner.invoke(cli, ["app-version", "load", str(versions_file), "--scenario-id", "1"])
    assert result.exit_code == 1
    assert "versions" in result.output


def test_app_version_load_rejects_unknown_scenario_id(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    versions_file = _write_versions_yaml(tmp_path)
    result = runner.invoke(cli, ["app-version", "load", str(versions_file), "--scenario-id", "999"])
    assert result.exit_code == 1
    assert "no scenario with id=999" in result.output
    with get_session(get_engine()) as session:
        assert session.query(AppVersion).count() == 0


def test_app_version_load_inserts_every_version(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario_id = _seed_bare_scenario()
    versions_file = _write_versions_yaml(tmp_path)

    result = runner.invoke(
        cli, ["app-version", "load", str(versions_file), "--scenario-id", str(scenario_id)]
    )

    assert result.exit_code == 0
    assert "loaded 2 of 2 app versions" in result.output
    with get_session(get_engine()) as session:
        rows = session.query(AppVersion).order_by(AppVersion.id).all()
        assert [row.label for row in rows] == ["InvenTree DEBUG=True", "InvenTree DEBUG=False"]
        assert rows[0].script_path == "/abs/debug_true.py"
        assert rows[0].version_ref == "DEBUG=True"
        assert all(row.scenario_id == scenario_id for row in rows)
        # Optional keys left out entirely stay null, rather than becoming empty strings.
        assert rows[1].description is None
        assert rows[1].version_ref is None


def test_app_version_load_aborts_without_inserting_on_missing_required_key(
    runner, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    scenario_id = _seed_bare_scenario()
    # The first entry is complete; the second one is not, so nothing at all may land.
    versions_file = _write_versions_yaml(
        tmp_path,
        _VERSIONS_YAML + "  - label: InvenTree cache=False\n    script_path: /abs/cache_off.py\n",
    )

    result = runner.invoke(
        cli, ["app-version", "load", str(versions_file), "--scenario-id", str(scenario_id)]
    )

    assert result.exit_code == 1
    assert "version 2 is missing required key(s)" in result.output
    for key in ("reset_script", "compose_file", "health_url"):
        assert key in result.output
    with get_session(get_engine()) as session:
        assert session.query(AppVersion).count() == 0


def test_app_version_load_warns_and_skips_duplicate_label(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario_id = _seed_bare_scenario()
    versions_file = _write_versions_yaml(tmp_path)
    runner.invoke(
        cli, ["app-version", "load", str(versions_file), "--scenario-id", str(scenario_id)]
    )

    # Same file, one entry's script_path edited: the loader is insert-only, so the edit
    # must be reported as skipped, not silently applied to the existing row.
    edited = _VERSIONS_YAML.replace("/abs/debug_true.py", "/abs/debug_true_edited.py")
    result = runner.invoke(
        cli,
        [
            "app-version",
            "load",
            str(_write_versions_yaml(tmp_path, edited)),
            "--scenario-id",
            str(scenario_id),
        ],
    )

    assert result.exit_code == 0
    assert "skipping 'InvenTree DEBUG=True'" in result.output
    assert "insert-only" in result.output
    assert "loaded 0 of 2 app versions" in result.output
    assert "2 skipped" in result.output
    with get_session(get_engine()) as session:
        rows = session.query(AppVersion).all()
        assert len(rows) == 2
        assert {row.script_path for row in rows} == {"/abs/debug_true.py", "/abs/debug_false.py"}


def test_app_version_load_still_inserts_new_entries_alongside_existing_ones(
    runner, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    scenario_id = _seed_bare_scenario()
    versions_file = _write_versions_yaml(tmp_path)
    runner.invoke(
        cli, ["app-version", "load", str(versions_file), "--scenario-id", str(scenario_id)]
    )

    # The realistic workflow: append a version to the same file and reload it.
    grown = _VERSIONS_YAML + (
        "  - label: InvenTree cache=False\n"
        "    script_path: /abs/cache_off.py\n"
        "    reset_script: /abs/reset_cache_off.sh\n"
        "    compose_file: /abs/cache-off/docker-compose.yml\n"
        "    health_url: http://localhost:8380/api/\n"
    )
    result = runner.invoke(
        cli,
        [
            "app-version",
            "load",
            str(_write_versions_yaml(tmp_path, grown)),
            "--scenario-id",
            str(scenario_id),
        ],
    )

    assert result.exit_code == 0
    assert "loaded 1 of 3 app versions" in result.output
    with get_session(get_engine()) as session:
        labels = [row.label for row in session.query(AppVersion).order_by(AppVersion.id).all()]
        assert labels[-1] == "InvenTree cache=False"
        assert len(labels) == 3


def test_app_version_load_skips_a_label_duplicated_within_one_file(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scenario_id = _seed_bare_scenario()
    doubled = _VERSIONS_YAML + (
        "  - label: InvenTree DEBUG=True\n"
        "    script_path: /abs/typo.py\n"
        "    reset_script: /abs/reset_typo.sh\n"
        "    compose_file: /abs/typo/docker-compose.yml\n"
        "    health_url: http://localhost:8580/api/\n"
    )

    result = runner.invoke(
        cli,
        [
            "app-version",
            "load",
            str(_write_versions_yaml(tmp_path, doubled)),
            "--scenario-id",
            str(scenario_id),
        ],
    )

    assert result.exit_code == 0
    assert "loaded 2 of 3 app versions" in result.output
    with get_session(get_engine()) as session:
        assert session.query(AppVersion).count() == 2


def test_app_version_load_allows_the_same_label_under_a_different_scenario(
    runner, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    first_scenario_id = _seed_bare_scenario()
    second_scenario_id = _seed_bare_scenario()
    versions_file = _write_versions_yaml(tmp_path)

    runner.invoke(
        cli, ["app-version", "load", str(versions_file), "--scenario-id", str(first_scenario_id)]
    )
    result = runner.invoke(
        cli, ["app-version", "load", str(versions_file), "--scenario-id", str(second_scenario_id)]
    )

    assert result.exit_code == 0
    assert "loaded 2 of 2 app versions" in result.output
    assert "skipping" not in result.output
    with get_session(get_engine()) as session:
        assert session.query(AppVersion).count() == 4


def _seed_scenario_and_versions(tmp_path):
    with get_session(get_engine()) as session:
        scenario = Scenario(name="s", steps=["create_part"])
        session.add(scenario)
        session.flush()
        version_a = AppVersion(
            scenario_id=scenario.id,
            label="a",
            script_path="a.py",
            reset_script="reset_a.sh",
            compose_file="a/docker-compose.yml",
            health_url="http://localhost:1/",
        )
        version_b = AppVersion(
            scenario_id=scenario.id,
            label="b",
            script_path="b.py",
            reset_script="reset_b.sh",
            compose_file="b/docker-compose.yml",
            health_url="http://localhost:2/",
        )
        session.add_all([version_a, version_b])
        session.commit()
        return scenario.id, version_a.id, version_b.id


def test_run_session_missing_row_errors(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("metrion_binary_path: /x\n")
    result = runner.invoke(
        cli,
        [
            "run-session",
            "--scenario-id",
            "999",
            "--version-a-id",
            "998",
            "--version-b-id",
            "997",
            "--repetitions",
            "1",
        ],
    )
    assert result.exit_code == 1
    assert "must reference existing rows" in result.output


def test_run_session_rejects_a_version_from_another_scenario(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("metrion_binary_path: /x\n")
    scenario_id, version_a_id, _version_b_id = _seed_scenario_and_versions(tmp_path)
    other_scenario_id, other_version_id, _ = _seed_scenario_and_versions(tmp_path)

    called = []
    monkeypatch.setattr("metrilens.cli.load_session_config", lambda *a, **k: object())
    monkeypatch.setattr("metrilens.cli.run_session_", lambda *a, **k: called.append(1))

    result = runner.invoke(
        cli,
        [
            "run-session",
            "--scenario-id",
            str(scenario_id),
            "--version-a-id",
            str(version_a_id),
            "--version-b-id",
            str(other_version_id),
            "--repetitions",
            "2",
        ],
    )

    assert result.exit_code == 1
    assert f"app_version {other_version_id} does not belong to scenario {scenario_id}" in (
        result.output
    )
    assert not called
    assert other_scenario_id != scenario_id


def test_run_session_invokes_orchestrator_and_prints_result(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("metrion_binary_path: /x\n")
    scenario_id, version_a_id, version_b_id = _seed_scenario_and_versions(tmp_path)

    monkeypatch.setattr("metrilens.cli.load_session_config", lambda *a, **k: object())
    fake_session = SimpleNamespace(id=42, execution_order=[version_a_id, version_b_id])
    monkeypatch.setattr("metrilens.cli.run_session_", lambda *a, **k: fake_session)

    result = runner.invoke(
        cli,
        [
            "run-session",
            "--scenario-id",
            str(scenario_id),
            "--version-a-id",
            str(version_a_id),
            "--version-b-id",
            str(version_b_id),
            "--repetitions",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert "id=42" in result.output


def test_run_session_orchestrator_error_exits_nonzero(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("metrion_binary_path: /x\n")
    scenario_id, version_a_id, version_b_id = _seed_scenario_and_versions(tmp_path)

    monkeypatch.setattr("metrilens.cli.load_session_config", lambda *a, **k: object())

    def fake_run_session(*a, **k):
        raise OrchestratorError("pre-flight equivalence_check 1 is FLAGGED")

    monkeypatch.setattr("metrilens.cli.run_session_", fake_run_session)

    result = runner.invoke(
        cli,
        [
            "run-session",
            "--scenario-id",
            str(scenario_id),
            "--version-a-id",
            str(version_a_id),
            "--version-b-id",
            str(version_b_id),
            "--repetitions",
            "2",
        ],
    )

    assert result.exit_code == 1
    assert "FLAGGED" in result.output


def test_retry_missing_repetition_errors(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("metrion_binary_path: /x\n")
    result = runner.invoke(cli, ["retry", "--repetition-id", "999"])
    assert result.exit_code == 1
    assert "no repetition with id=999" in result.output


def test_retry_invokes_orchestrator_and_prints_status(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("metrion_binary_path: /x\n")

    with get_session(get_engine()) as session:
        scenario = Scenario(name="s", steps=["create_part"])
        session.add(scenario)
        session.flush()
        version = AppVersion(
            scenario_id=scenario.id,
            label="a",
            script_path="a.py",
            reset_script="reset_a.sh",
            compose_file="a/docker-compose.yml",
            health_url="http://localhost:1/",
        )
        session.add(version)
        session.flush()
        comparison_session = ComparisonSession(
            scenario_id=scenario.id,
            host_descriptor="x",
            metrion_config_path="c.yaml",
            idle_power_calibration={},
            calibration_timestamp=datetime.now(UTC),
            execution_order=[version.id],
            random_seed=1,
        )
        session.add(comparison_session)
        session.flush()
        version_run = VersionRun(
            comparison_session_id=comparison_session.id,
            app_version_id=version.id,
            repetition_count_planned=1,
        )
        session.add(version_run)
        session.flush()
        repetition = Repetition(version_run_id=version_run.id, repetition_index=0)
        repetition.fail_measurement()
        session.add(repetition)
        session.commit()
        repetition_id = repetition.id

    monkeypatch.setattr("metrilens.cli.load_session_config", lambda *a, **k: object())
    fake_result = SimpleNamespace(id=repetition_id, status=RepetitionStatus.OK)
    monkeypatch.setattr("metrilens.cli.retry_repetition", lambda *a, **k: fake_result)

    result = runner.invoke(cli, ["retry", "--repetition-id", str(repetition_id)])

    assert result.exit_code == 0
    assert f"repetition {repetition_id} is now OK" in result.output


class TestAppVersionPathResolution:
    """The tracked definition carries relative paths, the database stores absolute."""

    _RELATIVE_YAML = """versions:
  - label: Baseline
    script_path: scenarios/inventree_session_flow/debug_false.py
    reset_script: scenarios/inventree_session_flow/reset_debug_false.sh
    compose_file: inventree-baseline/docker-compose.yml
    health_url: http://localhost:8180/api/
"""

    def _load(self, runner, tmp_path, monkeypatch, yaml_text, stacks_dir="/srv/stacks"):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app_versions.yaml").write_text(yaml_text)
        if stacks_dir is not None:
            (tmp_path / "config.yaml").write_text(f"stacks_dir: {stacks_dir}\n")
        scenario_id = _seed_bare_scenario()
        result = runner.invoke(
            cli,
            ["app-version", "load", "app_versions.yaml", "--scenario-id", str(scenario_id)],
        )
        return result

    def _row(self):
        with get_session(get_engine()) as session:
            return session.query(AppVersion).one()

    def test_relative_paths_are_stored_absolute(self, runner, tmp_path, monkeypatch):
        result = self._load(runner, tmp_path, monkeypatch, self._RELATIVE_YAML)
        assert result.exit_code == 0, result.output
        row = self._row()
        # Repo-relative for what lives in the checkout, stacks_dir for the compose file.
        assert row.script_path.endswith("scenarios/inventree_session_flow/debug_false.py")
        assert Path(row.script_path).is_absolute()
        assert Path(row.script_path).is_file()
        assert row.compose_file == "/srv/stacks/inventree-baseline/docker-compose.yml"

    def test_absolute_paths_still_pass_through_unchanged(self, runner, tmp_path, monkeypatch):
        absolute = """versions:
  - label: Baseline
    script_path: /abs/debug_false.py
    reset_script: /abs/reset.sh
    compose_file: /abs/stack/docker-compose.yml
    health_url: http://localhost:8180/api/
"""
        result = self._load(runner, tmp_path, monkeypatch, absolute)
        assert result.exit_code == 0, result.output
        row = self._row()
        assert row.script_path == "/abs/debug_false.py"
        assert row.compose_file == "/abs/stack/docker-compose.yml"

    def test_a_relative_compose_file_without_stacks_dir_fails_clearly(
        self, runner, tmp_path, monkeypatch
    ):
        # The one combination that cannot be resolved. It must say so rather than silently
        # storing a path relative to whatever the working directory happened to be, and
        # resolution happens before the database is touched, for the same reason key
        # validation does: a half-loaded file is worse than one that did not load.
        result = self._load(runner, tmp_path, monkeypatch, self._RELATIVE_YAML, stacks_dir=None)
        assert result.exit_code != 0
        assert "compose_file" in result.output
        assert "stacks_dir" in result.output
        with get_session(get_engine()) as session:
            assert session.query(AppVersion).count() == 0


class TestRecord:
    """`metrilens record` produces an illustrative video and, deliberately, nothing
    else: no calibration, no METRION, and no rows, so a recording can never be mistaken for a
    measurement or leave one behind."""

    @pytest.fixture
    def recorded(self, monkeypatch, tmp_path):
        """Stand in for the dry run, capturing what each version was recorded with."""
        calls: list[tuple[str, Path, dict]] = []

        def fake_dry_run(version, config, run_dir, extra_env=None):
            calls.append((version.label, run_dir, extra_env))

        monkeypatch.setattr("metrilens.cli.run_dry_run", fake_dry_run)
        # Same stand-in the `run-session` tests use: the config's METRION paths are validated
        # on load and belong to a machine, not to what this command decides.
        monkeypatch.setattr("metrilens.cli.load_session_config", lambda *a, **k: object())
        (tmp_path / "config.yaml").write_text("metrion_binary_path: /x\n")
        return calls

    def test_it_records_both_versions_with_their_own_label_and_directory(
        self, runner, tmp_path, monkeypatch, recorded
    ):
        monkeypatch.chdir(tmp_path)
        scenario_id, version_a_id, version_b_id = _seed_scenario_and_versions(tmp_path)

        result = runner.invoke(
            cli,
            [
                "record",
                "--scenario-id",
                str(scenario_id),
                "--version-a-id",
                str(version_a_id),
                "--version-b-id",
                str(version_b_id),
            ],
        )

        assert result.exit_code == 0
        assert [label for label, _, _ in recorded] == ["a", "b"]
        for label, run_dir, extra_env in recorded:
            assert run_dir.name == label
            assert extra_env[recording.VIDEO_DIR_ENV] == str(run_dir)
            assert extra_env[recording.LABEL_ENV] == label
            assert extra_env[recording.CAPTIONS_ENV] == "1"

    def test_one_arm_can_be_re_recorded_on_its_own(self, runner, tmp_path, monkeypatch, recorded):
        monkeypatch.chdir(tmp_path)
        scenario_id, version_a_id, _ = _seed_scenario_and_versions(tmp_path)

        result = runner.invoke(
            cli,
            ["record", "--scenario-id", str(scenario_id), "--version-a-id", str(version_a_id)],
        )

        assert result.exit_code == 0
        assert [label for label, _, _ in recorded] == ["a"]

    def test_no_captions_reaches_the_script(self, runner, tmp_path, monkeypatch, recorded):
        monkeypatch.chdir(tmp_path)
        scenario_id, version_a_id, _ = _seed_scenario_and_versions(tmp_path)

        runner.invoke(
            cli,
            [
                "record",
                "--scenario-id",
                str(scenario_id),
                "--version-a-id",
                str(version_a_id),
                "--no-captions",
            ],
        )

        ((_, _, extra_env),) = recorded
        assert extra_env[recording.CAPTIONS_ENV] == "0"

    def test_it_writes_no_rows(self, runner, tmp_path, monkeypatch, recorded):
        """The delineation from a measurement session, made checkable: a recording leaves no
        trace in the database."""
        monkeypatch.chdir(tmp_path)
        scenario_id, version_a_id, version_b_id = _seed_scenario_and_versions(tmp_path)

        runner.invoke(
            cli,
            [
                "record",
                "--scenario-id",
                str(scenario_id),
                "--version-a-id",
                str(version_a_id),
                "--version-b-id",
                str(version_b_id),
            ],
        )

        with get_session(get_engine()) as session:
            assert session.query(ComparisonSession).count() == 0
            assert session.query(Repetition).count() == 0

    def test_a_version_from_another_scenario_is_refused(
        self, runner, tmp_path, monkeypatch, recorded
    ):
        """Same check `run-session` makes: another scenario's version runs a different flow,
        so the video would carry this scenario's name over the wrong workload."""
        monkeypatch.chdir(tmp_path)
        scenario_id, version_a_id, _ = _seed_scenario_and_versions(tmp_path)

        result = runner.invoke(
            cli,
            [
                "record",
                "--scenario-id",
                str(scenario_id + 1),
                "--version-a-id",
                str(version_a_id),
            ],
        )

        assert result.exit_code == 1
        assert recorded == []

    def test_a_missing_row_is_refused(self, runner, tmp_path, monkeypatch, recorded):
        monkeypatch.chdir(tmp_path)
        scenario_id, version_a_id, _ = _seed_scenario_and_versions(tmp_path)

        result = runner.invoke(
            cli,
            [
                "record",
                "--scenario-id",
                str(scenario_id),
                "--version-a-id",
                str(version_a_id),
                "--version-b-id",
                "999",
            ],
        )

        assert result.exit_code == 1
        assert "must reference existing rows" in result.output
        assert recorded == []

    def test_the_slowed_companion_is_written_by_default(
        self, runner, tmp_path, monkeypatch, recorded
    ):
        monkeypatch.chdir(tmp_path)
        scenario_id, version_a_id, _ = _seed_scenario_and_versions(tmp_path)
        slowed: list[tuple[Path, float]] = []
        monkeypatch.setattr(
            "metrilens.cli.recording.slow_copy",
            lambda video, factor: slowed.append((video, factor)) or video,
        )

        result = runner.invoke(
            cli,
            ["record", "--scenario-id", str(scenario_id), "--version-a-id", str(version_a_id)],
        )

        assert result.exit_code == 0
        ((_, factor),) = slowed
        assert factor == 4.0

    def test_slowdown_one_records_the_capture_alone(self, runner, tmp_path, monkeypatch, recorded):
        monkeypatch.chdir(tmp_path)
        scenario_id, version_a_id, _ = _seed_scenario_and_versions(tmp_path)
        slowed: list[tuple[Path, float]] = []
        monkeypatch.setattr(
            "metrilens.cli.recording.slow_copy",
            lambda video, factor: slowed.append((video, factor)) or video,
        )

        runner.invoke(
            cli,
            [
                "record",
                "--scenario-id",
                str(scenario_id),
                "--version-a-id",
                str(version_a_id),
                "--slowdown",
                "1",
            ],
        )

        assert slowed == []

    def test_a_failed_slowdown_keeps_the_recording_and_the_exit_code(
        self, runner, tmp_path, monkeypatch, recorded
    ):
        """By then a stack has been reset and a scenario has run; the capture stands on its own
        and any factor can be re-derived from it later."""
        monkeypatch.chdir(tmp_path)
        scenario_id, version_a_id, _ = _seed_scenario_and_versions(tmp_path)

        def fail(video, factor):
            raise recording.RecordingError("no ffmpeg found")

        monkeypatch.setattr("metrilens.cli.recording.slow_copy", fail)

        result = runner.invoke(
            cli,
            ["record", "--scenario-id", str(scenario_id), "--version-a-id", str(version_a_id)],
        )

        assert result.exit_code == 0
        assert "no slowed copy written: no ffmpeg found" in result.output
        assert len(recorded) == 1
