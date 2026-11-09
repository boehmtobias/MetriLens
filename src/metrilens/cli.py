"""`click` CLI entrypoint for MetriLens."""

import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

import click
import yaml

from metrilens import recording
from metrilens.db import get_engine, get_session
from metrilens.evaluation import EvaluationError, run_evaluation
from metrilens.models import AppVersion, ComparisonSession, Repetition, Scenario
from metrilens.orchestrator import (
    REPO_ROOT,
    OrchestratorError,
    load_session_config,
    load_stacks_dir,
    retry_repetition,
    run_dry_run,
)
from metrilens.orchestrator import run_session as run_session_

_config_option = click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default="config.yaml",
    help="MetriLens's own YAML config.",
)
_data_dir_option = click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default="data/raw",
    help="Where per-repetition artifacts (step logs, METRION exports) are archived.",
)
_host_descriptor_option = click.option(
    "--host-descriptor",
    default=socket.gethostname,
    help="Which machine this session/retry runs on; recorded on comparison_session.",
)
# The non-null `app_version` columns; `description`/`version_ref` are optional.
_REQUIRED_VERSION_KEYS = ("label", "script_path", "reset_script", "compose_file", "health_url")


def _resolve_version_path(raw: str, *, key: str, base: Path | None, base_name: str) -> str:
    """Resolve one `app_versions.yaml` path to an absolute one.

    Absolute values pass through unchanged. A relative value is resolved against `base`, so the
    tracked file can describe where things sit *within* a machine's layout rather than naming one
    machine's directories: `script_path` and `reset_script` point inside the repository, and
    `compose_file` points inside whatever `stacks_dir` this machine configured.

    The `app_version` row stores the resolved absolute path, so a repetition still says exactly
    what it measured after any config changes.
    """
    path = Path(raw).expanduser()
    if path.is_absolute():
        return str(path)
    if base is None:
        raise click.ClickException(
            f"'{key}' is relative ({raw!r}) but {base_name} is not set; either make the path "
            "absolute or set it in config.yaml"
        )
    return str((base / path).resolve())


@click.group()
def cli() -> None:
    """MetriLens: measurement-and-comparison harness built on METRION."""


@cli.command("run-session")
@click.option("--scenario-id", type=int, required=True, help="Scenario to measure.")
@click.option(
    "--version-a-id",
    type=int,
    required=True,
    help=(
        "Baseline app version. Every difference in the generated report is reported relative "
        "to this one, so pass the unmodified app version here."
    ),
)
@click.option(
    "--version-b-id",
    type=int,
    required=True,
    help="App version to compare against the baseline.",
)
@click.option(
    "--repetitions",
    type=int,
    required=True,
    help="Repetitions planned per app version (version_run.repetition_count_planned).",
)
@click.option("--seed", type=int, default=None, help="Fix execution_order's random seed.")
@_config_option
@_data_dir_option
@_host_descriptor_option
def run_session(
    scenario_id: int,
    version_a_id: int,
    version_b_id: int,
    repetitions: int,
    seed: int | None,
    config_path: Path,
    data_dir: Path,
    host_descriptor: str,
) -> None:
    """Run a full measurement session: pre-flight gate, calibration, repetition loop."""
    session = get_session(get_engine())
    with session:
        scenario = session.get(Scenario, scenario_id)
        version_a = session.get(AppVersion, version_a_id)
        version_b = session.get(AppVersion, version_b_id)
        if scenario is None or version_a is None or version_b is None:
            click.echo(
                "scenario_id/version_a_id/version_b_id must reference existing rows", err=True
            )
            sys.exit(1)

        # Both versions must realize the scenario being measured. Existence alone is not enough:
        # a version from another scenario runs a different workload, so the pre-flight equivalence
        # gate would compare two unrelated step logs and the session would record the mismatch
        # against this scenario's id.
        foreign = [
            version.id for version in (version_a, version_b) if version.scenario_id != scenario_id
        ]
        if foreign:
            click.echo(
                f"app_version {'/'.join(str(version_id) for version_id in foreign)} "
                f"does not belong to scenario {scenario_id}",
                err=True,
            )
            sys.exit(1)

        config = load_session_config(config_path, data_dir, host_descriptor)
        try:
            comparison_session = run_session_(
                session, scenario, version_a, version_b, repetitions, config, seed=seed
            )
        except OrchestratorError as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
        click.echo(
            f"session complete (comparison_session id={comparison_session.id}, "
            f"execution_order={comparison_session.execution_order})"
        )


@cli.command("retry")
@click.option(
    "--repetition-id", type=int, required=True, help="Repetition left in a terminal failed state."
)
@_config_option
@_data_dir_option
@_host_descriptor_option
def retry(repetition_id: int, config_path: Path, data_dir: Path, host_descriptor: str) -> None:
    """Manually retry a repetition after `max_retries` auto-retries were exhausted."""
    session = get_session(get_engine())
    with session:
        repetition = session.get(Repetition, repetition_id)
        if repetition is None:
            click.echo(f"no repetition with id={repetition_id}", err=True)
            sys.exit(1)
        version = repetition.version_run.app_version

        config = load_session_config(config_path, data_dir, host_descriptor)
        run_dir = (
            data_dir / "retry" / str(repetition_id) / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        )
        repetition = retry_repetition(session, repetition, version, config, run_dir)
        click.echo(f"repetition {repetition.id} is now {repetition.status.value}")


@cli.command("report")
@click.option("--scenario-id", type=int, required=True)
@click.option("--comparison-session-id", type=int, required=True)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default="evaluation",
    help="Root directory for generated reports.",
)
def report(scenario_id: int, comparison_session_id: int, output_dir: Path) -> None:
    """Generate the comparison report/figures for a finished session."""
    session = get_session(get_engine())
    with session:
        comparison_session = session.get(ComparisonSession, comparison_session_id)
        if comparison_session is None or comparison_session.scenario_id != scenario_id:
            click.echo(
                f"no comparison_session with id={comparison_session_id} "
                f"for scenario_id={scenario_id}",
                err=True,
            )
            sys.exit(1)

        try:
            report_dir = run_evaluation(session, comparison_session_id, output_root=output_dir)
        except EvaluationError as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
        click.echo(f"report written to {report_dir}")


def _record_version(
    version: AppVersion, config, out_root: Path, *, captions: bool
) -> tuple[Path, Path]:
    """Record one app version, returning the video and the step log it was captioned from.

    The body is `run_dry_run` and nothing else, which is the point: a recording is the
    pre-flight pass (reset, readiness, the same script invoked the same way) with a
    screencast attached, so what the video shows cannot drift from what a session runs.
    METRION is never involved and no rows are written.
    """
    run_dir = out_root / recording.slug(version.label)
    run_dry_run(
        version,
        config,
        run_dir,
        extra_env=recording.environment(run_dir, version.label, captions),
    )
    return recording.video_path(run_dir, version.label), run_dir / "step_log.json"


@cli.command("record")
@click.option("--scenario-id", type=int, required=True, help="Scenario to record.")
@click.option("--version-a-id", type=int, required=True, help="App version to record first.")
@click.option(
    "--version-b-id",
    type=int,
    default=None,
    help="Second app version to record. Omit to record a single app version.",
)
@click.option(
    "--no-captions",
    is_flag=True,
    help="Leave out the step caption bar, for clean footage of the UI alone.",
)
@click.option(
    "--slowdown",
    type=float,
    default=recording.DEFAULT_SLOWDOWN,
    show_default=True,
    help="Also write a companion video this many times slower. Pass 1 for the capture alone.",
)
@_config_option
@_data_dir_option
@_host_descriptor_option
def record(
    scenario_id: int,
    version_a_id: int,
    version_b_id: int | None,
    no_captions: bool,
    slowdown: float,
    config_path: Path,
    data_dir: Path,
    host_descriptor: str,
) -> None:
    """Record an illustrative video of the scenario running against each version.

    Takes the same ids as `run-session` on purpose, so recording what a session measured is
    that session's command with the verb swapped, read off the same `app_version` rows rather
    than a parallel config that could point somewhere else.

    Explicitly not a measurement: no calibration, no `metrion monitor`, no `comparison_session`
    or `repetition` rows, and the screencast's own CPU cost makes the recorded run slower than
    the same run unrecorded. It is for showing what the scenario does, not what it costs.
    """
    session = get_session(get_engine())
    with session:
        scenario_row = session.get(Scenario, scenario_id)
        version_ids = [version_a_id] + ([version_b_id] if version_b_id is not None else [])
        versions = [session.get(AppVersion, version_id) for version_id in version_ids]
        if scenario_row is None or any(version is None for version in versions):
            click.echo(
                "scenario-id/version-a-id/version-b-id must reference existing rows", err=True
            )
            sys.exit(1)

        # Same check as `run-session`: a version from another scenario runs a different workload,
        # so recording it under this scenario's name would label the video with the wrong flow.
        foreign = [version.id for version in versions if version.scenario_id != scenario_id]
        if foreign:
            click.echo(
                f"app_version {'/'.join(str(version_id) for version_id in foreign)} "
                f"does not belong to scenario {scenario_id}",
                err=True,
            )
            sys.exit(1)

        config = load_session_config(config_path, data_dir, host_descriptor)
        out_root = data_dir / "recordings" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for version in versions:
            click.echo(f"recording '{version.label}' (this resets its stack first)")
            try:
                video_path, step_log_path = _record_version(
                    version, config, out_root, captions=not no_captions
                )
            except OrchestratorError as exc:
                click.echo(str(exc), err=True)
                sys.exit(1)
            click.echo(f"  {video_path}")
            click.echo(f"  {step_log_path}")
            if slowdown > 1:
                # Warned about rather than fatal: the capture has already cost a stack reset and
                # a full scenario run by the time this can fail, and the video it produced is
                # fine on its own. Any factor can be re-derived from it later.
                try:
                    click.echo(f"  {recording.slow_copy(video_path, slowdown)}")
                except recording.RecordingError as exc:
                    click.echo(f"  no slowed copy written: {exc}", err=True)


@cli.group("scenario")
def scenario() -> None:
    """Manage scenario definitions."""


_SCENARIO_KEYS = {"name", "description", "steps", "iterations"}


@scenario.command("load")
@click.argument("path", type=click.Path(exists=True))
def scenario_load(path: str) -> None:
    """Load a scenario definition (YAML) into the database."""
    definition = yaml.safe_load(Path(path).read_text()) or {}
    name = definition.get("name")
    steps = definition.get("steps")
    if not name or not steps:
        click.echo(f"'{path}' is missing required key 'name' or 'steps'", err=True)
        sys.exit(1)

    # An unknown key is rejected rather than ignored. `iterations` is the reason: a scenario
    # that declares k and gets loaded into a row without it would run its k iterations and
    # report a headline number that cannot be interpreted, with nothing anywhere saying so.
    # A typo in that key would do exactly that silently.
    unknown = sorted(set(definition) - _SCENARIO_KEYS)
    if unknown:
        click.echo(
            f"'{path}' has unknown key(s) {unknown}; expected any of {sorted(_SCENARIO_KEYS)}",
            err=True,
        )
        sys.exit(1)

    iterations = definition.get("iterations", 1)
    if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < 1:
        click.echo(
            f"'{path}' has 'iterations' = {iterations!r}; expected an integer >= 1", err=True
        )
        sys.exit(1)

    session = get_session(get_engine())
    with session:
        scenario_row = Scenario(
            name=name,
            description=definition.get("description"),
            steps=steps,
            iterations=iterations,
        )
        session.add(scenario_row)
        session.commit()
        click.echo(
            f"loaded scenario '{name}' (id={scenario_row.id}, {len(steps)} steps, "
            f"{iterations} iteration(s) per repetition)"
        )


@cli.group("app-version")
def app_version() -> None:
    """Manage app version definitions."""


@app_version.command("load")
@click.argument("path", type=click.Path(exists=True))
@click.option("--scenario-id", type=int, required=True, help="Scenario these versions realize.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default="config.yaml",
    show_default=True,
    help="MetriLens's own YAML config, read for `stacks_dir`.",
)
def app_version_load(path: str, scenario_id: int, config_path: Path) -> None:
    """Load a scenario folder's `app_versions.yaml` into the database.

    Insert-only, deliberately not an upsert-by-label: `version_run.app_version_id` is a
    foreign key every repetition traces back to, so editing a row in place would silently
    rewrite what a past session already on record actually measured. A changed script,
    compose file, or health URL is a new app version and needs a new row under a new label.
    """
    definition = yaml.safe_load(Path(path).read_text()) or {}
    versions = definition.get("versions")
    if not versions:
        click.echo(f"'{path}' is missing required key 'versions'", err=True)
        sys.exit(1)

    # Validate every entry before touching the database: a file that half-loads is worse
    # than one that does not load at all, since nothing on screen says what landed.
    for index, version in enumerate(versions):
        missing = [key for key in _REQUIRED_VERSION_KEYS if not (version or {}).get(key)]
        if missing:
            click.echo(
                f"'{path}' version {index} is missing required key(s): {', '.join(missing)}",
                err=True,
            )
            sys.exit(1)

    # Resolved before the database is touched, for the same reason the keys are validated
    # first: a file that half-loads is worse than one that does not load at all.
    stacks_dir = load_stacks_dir(config_path) if config_path.is_file() else None
    resolved: list[dict[str, str]] = []
    for version in versions:
        resolved.append(
            {
                "script_path": _resolve_version_path(
                    version["script_path"],
                    key="script_path",
                    base=REPO_ROOT,
                    base_name="the repository root",
                ),
                "reset_script": _resolve_version_path(
                    version["reset_script"],
                    key="reset_script",
                    base=REPO_ROOT,
                    base_name="the repository root",
                ),
                "compose_file": _resolve_version_path(
                    version["compose_file"],
                    key="compose_file",
                    base=stacks_dir,
                    base_name="'stacks_dir'",
                ),
            }
        )

    session = get_session(get_engine())
    with session:
        if session.get(Scenario, scenario_id) is None:
            click.echo(f"no scenario with id={scenario_id}", err=True)
            sys.exit(1)

        # Labels already on record for this scenario only: the same label under a different
        # scenario is a different app entirely and no conflict at all.
        known_labels = {
            row.label: row.id
            for row in session.query(AppVersion).filter_by(scenario_id=scenario_id).all()
        }
        inserted = 0
        for version, paths in zip(versions, resolved, strict=True):
            label = version["label"]
            if label in known_labels:
                click.echo(
                    f"skipping '{label}': label already exists for scenario {scenario_id} "
                    f"(app_version id={known_labels[label]}); this loader is insert-only and "
                    "did not modify it, use a new label to record a changed configuration",
                    err=True,
                )
                continue
            version_row = AppVersion(
                scenario_id=scenario_id,
                label=label,
                description=version.get("description") or None,
                script_path=paths["script_path"],
                reset_script=paths["reset_script"],
                compose_file=paths["compose_file"],
                health_url=version["health_url"],
                version_ref=version.get("version_ref") or None,
            )
            session.add(version_row)
            session.flush()
            # Also guards against the same label appearing twice within this one file.
            known_labels[label] = version_row.id
            inserted += 1
            click.echo(f"loaded app_version '{label}' (id={version_row.id})")
        session.commit()

        # Printed after the per-entry lines so the outcome survives long scrollback.
        summary = f"loaded {inserted} of {len(versions)} app versions"
        skipped = len(versions) - inserted
        if skipped:
            summary += f" ({skipped} skipped: label already present for scenario {scenario_id})"
        click.echo(summary)


if __name__ == "__main__":
    cli()
