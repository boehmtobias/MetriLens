"""Runs one comparison session end to end.

The pre-flight dry run and equivalence gate, settling and idle-power calibration, the
randomized `execution_order`, and the repetition loop with its retries. The work itself is
delegated to `lifecycle`, `metrion_integration`, `validation`, `writer` and `equivalence`;
this module only sequences it.
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.orm import Session

from metrilens.equivalence import check_equivalence
from metrilens.host import (
    CPU_ROOT,
    collect_host_facts,
    describe_cpu_governors,
    read_cpu_governors,
)
from metrilens.lifecycle import (
    LifecycleError,
    ReadinessAuth,
    reset_target_app,
    wait_for_readiness,
)
from metrilens.metrion_integration import (
    DEFAULT_ANALYZE_WRAPPER_PATH,
    DEFAULT_BATCH_CLEANUP_SCRIPT_PATH,
    DEFAULT_MONITOR_STOP_TIMEOUT_S,
    MONITOR_LOG_NAME,
    MetrionBridgeError,
    MonitorKilledError,
    find_new_db_file,
    force_idle_calibration,
    load_analyze_wrapper_path,
    load_batch_cleanup_script_path,
    load_metrion_binary_path,
    load_metrion_config_path,
    load_metrion_db_dir,
    load_metrion_idle_log_path,
    load_metrion_output_dir,
    resolve_compose_container_map,
    resolve_compose_pids,
    run_analyze,
    snapshot_batch_files,
    start_monitor,
    stop_monitor,
)
from metrilens.models import (
    AppVersion,
    ComparisonSession,
    EquivalenceCheck,
    EquivalenceVerdict,
    Repetition,
    RepetitionStatus,
    Scenario,
    VersionRun,
)
from metrilens.validation import StepLog
from metrilens.writer import CanonicalCounts, write_repetition


class OrchestratorError(RuntimeError):
    """Raised when the pre-flight gate fails, or a script invocation itself fails."""


@dataclass
class SessionConfig:
    """Machine-specific paths/settings the orchestrator needs beyond the DB."""

    metrion_binary_path: Path
    metrion_config_path: Path
    metrion_output_dir: Path
    metrion_idle_log_path: Path
    metrion_db_dir: Path
    data_dir: Path
    host_descriptor: str
    readiness_timeout_s: float = 60
    # Separate from `readiness_timeout_s`: `reset_target_app`'s reset_script does a full
    # compose down/up plus its own migration-wait loop plus seed data, not the quick HTTP
    # poll `readiness_timeout_s` is sized for, so it needs real headroom.
    reset_timeout_s: float = 240
    max_retries: int = 3
    settle_time_s: float = 60
    # How long to keep `metrion monitor` running after the scenario script exits. The measured
    # window's end is rounded outward, so it can fall up to a second after the script's exit and
    # needs samples there.
    monitor_tail_wait_s: float = 3.0
    # How long `metrion monitor` gets to exit after SIGINT before it is killed. Per host, since it
    # depends on how long METRION takes to flush its database, not on the workload.
    monitor_stop_timeout_s: float = DEFAULT_MONITOR_STOP_TIMEOUT_S
    # Optional: absent means the readiness check stops at the HTTP endpoint, which is fine for
    # a target app with no login gating the workload.
    readiness_auth: ReadinessAuth | None = None
    # Script `sudo` runs to delete METRION's leftover `/tmp/batch_*.ipc` files. Defaults to the
    # in-checkout copy; where another account can write the checkout, point it at a root-owned
    # copy, since root executes it.
    batch_cleanup_script_path: Path = DEFAULT_BATCH_CLEANUP_SCRIPT_PATH
    # Root-owned wrapper `sudo` runs instead of `metrion analyze`. Same default and same caveat.
    analyze_wrapper_path: Path = DEFAULT_ANALYZE_WRAPPER_PATH


_READINESS_AUTH_KEYS = (
    "login_page_path",
    "login_path",
    "session_path",
    "username_env",
    "password_env",
)


def _load_readiness_auth(raw: dict) -> ReadinessAuth | None:
    """Build the optional `readiness_auth` block, or `None` when it is absent.

    A present but incomplete block is an error rather than being ignored, since ignoring it
    would silently skip the authenticated readiness check the config asked for.
    """
    block = raw.get("readiness_auth")
    if not block:
        return None
    missing = [key for key in _READINESS_AUTH_KEYS if not block.get(key)]
    if missing:
        raise OrchestratorError(
            f"config's 'readiness_auth' is missing required key(s): {', '.join(missing)}"
        )
    optional = {
        key: block[key] for key in ("csrf_cookie_name", "csrf_header_name") if block.get(key)
    }
    return ReadinessAuth(**{key: block[key] for key in _READINESS_AUTH_KEYS}, **optional)


# Repository root, for resolving the scenario definitions' relative paths.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_stacks_dir(config_path: Path) -> Path | None:
    """Read the optional `stacks_dir` from MetriLens's own YAML config.

    Where this machine keeps the target app's Compose projects. Deliberately here rather than
    in `app_versions.yaml`: that file is tracked and shared between machines, while this is the
    one genuinely machine-specific value the scenario definitions need, and every other
    machine-specific path in this project already lives in `config.yaml`.

    Optional, and `None` means the definitions must carry absolute `compose_file` paths.
    """
    raw = yaml.safe_load(config_path.read_text()) or {}
    value = raw.get("stacks_dir")
    return Path(value).expanduser() if value else None


def load_session_config(config_path: Path, data_dir: Path, host_descriptor: str) -> SessionConfig:
    """Build a `SessionConfig` from MetriLens's own YAML config plus session-specific values.

    Bundles the machine-specific METRION paths (each resolved through
    `metrion_integration`'s own loaders) with the orchestrator's small tunables,
    so a caller (the CLI) only deals with one config object.
    """
    raw = yaml.safe_load(config_path.read_text()) or {}
    return SessionConfig(
        metrion_binary_path=load_metrion_binary_path(config_path),
        metrion_config_path=load_metrion_config_path(config_path),
        metrion_output_dir=load_metrion_output_dir(config_path),
        metrion_idle_log_path=load_metrion_idle_log_path(config_path),
        metrion_db_dir=load_metrion_db_dir(config_path),
        batch_cleanup_script_path=load_batch_cleanup_script_path(config_path),
        analyze_wrapper_path=load_analyze_wrapper_path(config_path),
        data_dir=data_dir,
        host_descriptor=host_descriptor,
        readiness_timeout_s=raw.get("readiness_timeout_s", 60),
        reset_timeout_s=raw.get("reset_timeout_s", 240),
        max_retries=raw.get("max_retries", 3),
        settle_time_s=raw.get("settle_time_s", 60),
        monitor_tail_wait_s=raw.get("monitor_tail_wait_s", 3.0),
        monitor_stop_timeout_s=raw.get("monitor_stop_timeout_s", DEFAULT_MONITOR_STOP_TIMEOUT_S),
        readiness_auth=_load_readiness_auth(raw),
    )


_PACKAGE_HEADER_RE = re.compile(r"^===\s*Package\s+(\d+)\s*===$")
_PACKAGE_TOTAL_RE = re.compile(r"^Package Total\s*:\s*([\d.,]+)\s*W$")


def parse_idle_calibration(idle_log_path: Path) -> dict[str, float]:
    """Parse METRION's regenerated `rapl-idle.log` into `{"package_<n>": watts}`.

    The log's `Package Total` line can be comma-decimal depending on the host's locale;
    normalized to a plain float here rather than assumed already parseable, since
    `comparison_session.idle_power_calibration` needs the actual value, not just a
    path/timestamp.
    """
    values: dict[str, float] = {}
    current_package: str | None = None
    for line in Path(idle_log_path).read_text().splitlines():
        stripped = line.strip()
        header = _PACKAGE_HEADER_RE.match(stripped)
        if header:
            current_package = header.group(1)
            continue
        total = _PACKAGE_TOTAL_RE.match(stripped)
        if total and current_package is not None:
            values[f"package_{current_package}"] = float(total.group(1).replace(",", "."))
            current_package = None
    if not values:
        raise OrchestratorError(f"no package idle-power values found in {idle_log_path}")
    return values


def generate_execution_order(
    version_a_id: int, version_b_id: int, repetitions_per_version: int, seed: int | None = None
) -> tuple[list[int], int]:
    """A random permutation of which version measures at each of the `2N` slots.

    Randomized at repetition granularity rather than fixed/block-patterned, to
    avoid aliasing with any periodic effect the session might otherwise share
    with a fixed order.
    """
    if seed is None:
        seed = random.SystemRandom().randrange(2**32)
    rng = random.Random(seed)
    slots = [version_a_id] * repetitions_per_version + [version_b_id] * repetitions_per_version
    rng.shuffle(slots)
    return slots, seed


def _run_script(
    script_path: str, output_path: Path, extra_env: dict[str, str] | None = None
) -> None:
    """Invoke a version's Playwright script, writing its step log to `output_path`.

    The script never decides where its own step log goes; the caller supplies that
    path per repetition/dry-run.

    `extra_env` is added to the inherited environment, never substituted for it: the script
    resolves the target app's credentials from the environment it is handed. It is `None` for
    every measured repetition, and carries the recording contract (`metrilens record`)
    for the one caller that is not a measurement.
    """
    env = {**os.environ, **extra_env} if extra_env else None
    result = subprocess.run(
        [sys.executable, script_path, "--output", str(output_path)],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    if result.returncode != 0:
        raise OrchestratorError(
            f"'{script_path}' exited {result.returncode}: {result.stderr.strip()}"
        )


def run_dry_run(
    version: AppVersion,
    config: SessionConfig,
    run_dir: Path,
    extra_env: dict[str, str] | None = None,
) -> StepLog:
    """One unmeasured full reset-to-script pass for `version`.

    Smoke-tests the whole mechanism end to end and produces the step log the
    equivalence checker needs, without wrapping it in `metrion monitor`/`analyze`.

    `extra_env` exists so `metrilens record` can reuse this exact sequence rather than
    reimplementing reset, readiness and invocation: a recording is then the pre-flight pass
    with a screencast attached, by construction rather than by care.
    """
    reset_target_app(
        Path(version.compose_file), version.reset_script, timeout_s=config.reset_timeout_s
    )
    wait_for_readiness(
        Path(version.compose_file),
        version.health_url,
        config.readiness_timeout_s,
        config.readiness_auth,
    )
    step_log_path = run_dir / "step_log.json"
    _run_script(version.script_path, step_log_path, extra_env=extra_env)
    return StepLog.from_json_file(step_log_path)


def run_preflight_gate(
    session: Session,
    scenario: Scenario,
    version_a: AppVersion,
    version_b: AppVersion,
    config: SessionConfig,
) -> EquivalenceCheck:
    """Dry-run both versions once and compute the `equivalence_check` verdict.

    Only produces the verdict; whether the session proceeds past it is the
    caller's decision (`run_session`), so this stays independently testable/usable
    (e.g. re-checking a fixed script without necessarily starting a session).
    """
    dry_run_dir = config.data_dir / "preflight" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    step_log_a = run_dry_run(version_a, config, dry_run_dir / "a")
    step_log_b = run_dry_run(version_b, config, dry_run_dir / "b")
    return check_equivalence(
        session, scenario.id, version_a.id, version_b.id, step_log_a, step_log_b
    )


def start_comparison_session(
    session: Session,
    scenario: Scenario,
    version_a: AppVersion,
    version_b: AppVersion,
    config: SessionConfig,
    repetitions_per_version: int,
    seed: int | None = None,
) -> ComparisonSession:
    """Settle, calibrate, and determine execution order.

    Only meant to be called once `run_preflight_gate`'s verdict has already
    cleared the gate; deliberately does not check that itself, `run_session`
    owns that ordering so this stays a plain building block.
    """
    time.sleep(config.settle_time_s)
    # Beside the pre-flight's own directory and named the same way, since like that one this runs
    # before there is a `comparison_session` row to file it under.
    calibration_stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    calibration_dir = config.data_dir / "calibration" / calibration_stamp
    idle_log_path = force_idle_calibration(
        config.metrion_binary_path,
        config.metrion_config_path,
        config.metrion_idle_log_path,
        calibration_dir / MONITOR_LOG_NAME,
        cleanup_script_path=config.batch_cleanup_script_path,
    )
    idle_power_calibration = parse_idle_calibration(idle_log_path)
    execution_order, random_seed = generate_execution_order(
        version_a.id, version_b.id, repetitions_per_version, seed
    )

    comparison_session = ComparisonSession(
        scenario_id=scenario.id,
        host_descriptor=config.host_descriptor,
        metrion_config_path=str(config.metrion_config_path),
        idle_power_calibration=idle_power_calibration,
        calibration_timestamp=datetime.now(UTC),
        execution_order=execution_order,
        random_seed=random_seed,
        cpu_governor=describe_cpu_governors(read_cpu_governors(CPU_ROOT)),
        host_facts=collect_host_facts(config.metrion_binary_path),
        started_at=datetime.now(UTC),
    )
    session.add(comparison_session)
    session.flush()

    for version in (version_a, version_b):
        session.add(
            VersionRun(
                comparison_session_id=comparison_session.id,
                app_version_id=version.id,
                repetition_count_planned=repetitions_per_version,
            )
        )
    session.commit()
    return comparison_session


def _floor_second(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _ceil_second(value: datetime) -> datetime:
    return value if value.microsecond == 0 else value.replace(microsecond=0) + timedelta(seconds=1)


def _analyze_window(
    step_log_path: Path, process_start: datetime, process_end: datetime
) -> tuple[datetime, datetime, str | None]:
    """The measured window passed to `metrion analyze`: the declared steps, not the whole process.

    The process window wraps the scenario script's own process startup, browser launch, and
    login, none of which is part of the measured workload. Narrowing here is what keeps that
    time out of the reported energy.

    Rounded outward, because `analyze` selects intervals it fully contains. METRION samples
    in contiguous 1.000 s intervals and exports only those lying entirely inside the requested
    window, so a boundary moved inward would drop the interval straddling it, taking real
    workload with it, while rounding outward costs only a small, known overshoot near each
    boundary. `run_analyze` formats with second resolution, so the rounding has to happen here
    rather than being left to `strftime`, which would floor both ends and shift the window
    earlier.

    An outward end can land after `process_end`, which is why `monitor_tail_wait_s` keeps the
    monitor sampling past the script's exit: without it the last requested second could hold no
    samples.

    Falls back to the process window, with a note for the row, whenever the step span cannot be
    trusted: an unreadable step log, timestamps that are not timezone-aware, or a span that does
    not sit inside the process window. That last check matters because a badly offset timestamp
    would otherwise ask METRION for a window holding no samples at all, which produces an empty
    export and fails the repetition rather than an error naming the cause.
    """
    try:
        step_log = StepLog.from_json_file(step_log_path)
    except (ValidationError, OSError, ValueError) as exc:
        return process_start, process_end, f"analyze window: step log unreadable ({exc})"

    start, end = step_log.start_time, step_log.end_time
    if start.tzinfo is None or end.tzinfo is None:
        return process_start, process_end, "analyze window: step log timestamps are not UTC-aware"
    if not process_start <= start < end <= process_end:
        return (
            process_start,
            process_end,
            f"analyze window: step span {start.isoformat()} to {end.isoformat()} "
            "does not lie inside the process window",
        )

    # No degenerate case to guard once the rounding goes outward: the check above establishes
    # `start < end`, so flooring one and ceiling the other always leaves at least a whole second.
    return _floor_second(start), _ceil_second(end), None


def _fail_measurement(session: Session, repetition: Repetition, notes: str) -> Repetition:
    repetition.validation_notes = notes
    repetition.fail_measurement()
    session.add(repetition)
    session.commit()
    return repetition


def _archive_monitor_db(
    config: SessionConfig, run_dir: Path, db_dir_before: set[Path], monitor_started_at: float
) -> str:
    """Copy a failed attempt's METRION database into its run directory, for diagnosis.

    The success path copies it on the way to `analyze`; without this, a failed attempt's
    database would survive only in METRION's own working directory.

    Returns a note fragment for `validation_notes`, empty when the copy succeeded. Never raises,
    so a copy failure cannot mask the reason the attempt failed.
    """
    try:
        db_path = find_new_db_file(
            config.metrion_db_dir, db_dir_before, not_before=monitor_started_at
        )
        shutil.copy2(db_path, run_dir / "metrion.db")
    except (OSError, MetrionBridgeError) as exc:
        return f"; this attempt's metrion database could not be archived: {exc}"
    return ""


def _execute_repetition(
    session: Session,
    repetition: Repetition,
    version: AppVersion,
    config: SessionConfig,
    run_dir: Path,
    canonical_counts: CanonicalCounts | None,
) -> Repetition:
    """One attempt at an already-`RUNNING` repetition: reset, monitor, script, analyze, write."""
    repetition.measurement_started_at = datetime.now(UTC)
    session.add(repetition)
    session.flush()

    try:
        reset_target_app(
            Path(version.compose_file), version.reset_script, timeout_s=config.reset_timeout_s
        )
        wait_for_readiness(
            Path(version.compose_file),
            version.health_url,
            config.readiness_timeout_s,
            config.readiness_auth,
        )
        pids = resolve_compose_pids(Path(version.compose_file))
        container_map = resolve_compose_container_map(Path(version.compose_file))
    except (LifecycleError, MetrionBridgeError) as exc:
        return _fail_measurement(session, repetition, f"lifecycle failure: {exc}")

    db_dir_before = set(config.metrion_db_dir.glob("*.db"))
    batch_files_before = snapshot_batch_files()
    # Read before the monitor starts, so `find_new_db_file` can tell a database this attempt wrote
    # from one left behind earlier.
    monitor_started_at = time.time()
    try:
        monitor_process = start_monitor(
            config.metrion_binary_path, config.metrion_config_path, run_dir / MONITOR_LOG_NAME
        )
    except MetrionBridgeError as exc:
        return _fail_measurement(session, repetition, f"metrion monitor start failure: {exc}")

    start_time = datetime.now(UTC)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "container_map.json").write_text(json.dumps(container_map))
    step_log_path = run_dir / "step_log.json"
    try:
        _run_script(version.script_path, step_log_path)
    except OrchestratorError as exc:
        stop_note = ""
        try:
            stop_monitor(
                monitor_process,
                config.metrion_binary_path,
                timeout_s=config.monitor_stop_timeout_s,
                batch_files_before=batch_files_before,
                cleanup_script_path=config.batch_cleanup_script_path,
            )
        except MetrionBridgeError as stop_exc:
            stop_note = f"; additionally, metrion monitor stop failure: {stop_exc}"
        # Archived even though this attempt is failing: the monitor ran, so there is a database,
        # and this is the only place a failed attempt's own measurement survives.
        archive_note = _archive_monitor_db(config, run_dir, db_dir_before, monitor_started_at)
        return _fail_measurement(
            session, repetition, f"script failure: {exc}{stop_note}{archive_note}"
        )
    end_time = datetime.now(UTC)
    analyze_start, analyze_end, window_note = _analyze_window(step_log_path, start_time, end_time)
    if window_note:
        logger.warning("{} (repetition {})", window_note, repetition.id)

    # Keep sampling past the script's exit before stopping the monitor. `_analyze_window` rounds
    # the window end outward, so `analyze_end` can be up to a second after `end_time`, and
    # METRION exports only intervals it fully contains: without this wait that last second could
    # hold no samples. The wait is the same for both app versions.
    time.sleep(config.monitor_tail_wait_s)

    kill_note = None
    try:
        stop_monitor(
            monitor_process,
            config.metrion_binary_path,
            timeout_s=config.monitor_stop_timeout_s,
            batch_files_before=batch_files_before,
            cleanup_script_path=config.batch_cleanup_script_path,
            log_path=run_dir / MONITOR_LOG_NAME,
        )
    except MonitorKilledError as exc:
        # A kill is not a lost repetition. METRION's shutdown is slow rather than hung, so by the
        # time the grace period expires it has usually already written the whole window. So the
        # repetition continues and the *export* decides, through `write_repetition`'s coverage
        # check, which is what makes this safe: a monitor killed during startup leaves a fraction
        # of the window behind and fails validation there instead of landing as a
        # plausible-looking low reading.
        logger.warning("{} (repetition {}), continuing on export coverage", exc, repetition.id)
        kill_note = f"metrion monitor was killed after its grace period: {exc}"
    except MetrionBridgeError as exc:
        archive_note = _archive_monitor_db(config, run_dir, db_dir_before, monitor_started_at)
        return _fail_measurement(
            session, repetition, f"metrion monitor stop failure: {exc}{archive_note}"
        )

    archived_db_path = run_dir / "metrion.db"
    try:
        db_path = find_new_db_file(
            config.metrion_db_dir, db_dir_before, not_before=monitor_started_at
        )
        shutil.copy2(db_path, archived_db_path)
        # Diagnostic only: an "analyze reported success but wrote no new files" failure is hard
        # to reproduce manually with a reconstructed window/PID list, so log the exact values
        # actually used here.
        logger.debug(
            "run_analyze: db_path={} pids={} start_time={} end_time={}",
            db_path,
            pids,
            analyze_start.isoformat(),
            analyze_end.isoformat(),
        )
        archived = run_analyze(
            config.analyze_wrapper_path,
            db_path,
            analyze_start,
            analyze_end,
            config.metrion_output_dir,
            run_dir,
        )
    except (OSError, MetrionBridgeError) as exc:
        return _fail_measurement(session, repetition, f"metrion analyze failure: {exc}")

    export_path = next(
        (path for name, path in archived.items() if name.startswith("energy_stats_")), None
    )
    if export_path is None:
        return _fail_measurement(
            session, repetition, "metrion analyze produced no energy_stats_*.json export"
        )
    # The per-thread CPU export is not optional detail: it's where the repetition's own energy
    # comes from, because `analyze` is asked for the whole window and the target app's rows are
    # selected from it by cgroup.
    raw_cpu_path = next(
        (path for name, path in archived.items() if name.startswith("raw_cpu")), None
    )
    if raw_cpu_path is None:
        return _fail_measurement(
            session, repetition, "metrion analyze produced no raw_cpu_*.csv export"
        )
    raw_export_ref = ",".join(
        str(path) for name, path in sorted(archived.items()) if name.startswith("raw_")
    )

    written = write_repetition(
        session,
        repetition,
        metrion_export_path=export_path,
        raw_cpu_export_path=raw_cpu_path,
        container_map=container_map,
        step_log_path=step_log_path,
        metrion_db_ref=str(archived_db_path),
        raw_export_ref=raw_export_ref or str(export_path),
        canonical_counts=canonical_counts,
    )
    trailing_notes = [note for note in (written.validation_notes, window_note, kill_note) if note]
    if window_note or kill_note:
        # Appended after the writer rather than set before it: `write_repetition` assigns
        # `validation_notes` itself for count drift, so setting it earlier would be overwritten
        # and a fallback to the process window, or a killed monitor, would leave no trace at all.
        written.validation_notes = "; ".join(trailing_notes)
        session.add(written)
        session.commit()
    return written


def run_repetition(
    session: Session,
    version_run: VersionRun,
    version: AppVersion,
    repetition_index: int,
    config: SessionConfig,
    run_dir: Path,
    canonical_counts: CanonicalCounts | None = None,
) -> Repetition:
    """One fresh measurement repetition: create the `RUNNING` row, then run it."""
    repetition = Repetition(version_run_id=version_run.id, repetition_index=repetition_index)
    session.add(repetition)
    session.flush()
    return _execute_repetition(session, repetition, version, config, run_dir, canonical_counts)


def _record_attempt_history(
    session: Session, repetition: Repetition, history: list[str]
) -> Repetition:
    """Prepend what each failed attempt recorded to whatever the surviving attempt wrote.

    Every retry re-enters `_execute_repetition`, which either sets `validation_notes` itself or
    leaves the writer to set it, so a repetition that failed and then succeeded would otherwise
    end as `ok` with the column null and no record of what went wrong along the way. Accumulated
    rather than replaced, so the row says what it cost to get the measurement.

    Bookkeeping and provenance only: evaluation excludes every non-`ok` repetition already, so no
    reported number moves.
    """
    if not history:
        return repetition
    surviving = [repetition.validation_notes] if repetition.validation_notes else []
    repetition.validation_notes = "; ".join([*history, *surviving])
    session.add(repetition)
    session.commit()
    return repetition


def run_repetition_with_retries(
    session: Session,
    version_run: VersionRun,
    version: AppVersion,
    repetition_index: int,
    config: SessionConfig,
    run_dir: Path,
    canonical_counts: CanonicalCounts | None = None,
) -> Repetition:
    """Run one repetition, auto-retrying the same row up to `max_retries` times.

    A failed repetition does not halt the session. Stops retrying and leaves
    the row in its terminal failed state for manual review once `max_retries`
    is exhausted, maximizing data collected per unattended session while
    keeping every failure visible.
    """
    repetition = run_repetition(
        session, version_run, version, repetition_index, config, run_dir, canonical_counts
    )
    attempt = 0
    history: list[str] = []
    failed_states = (RepetitionStatus.FAILED_VALIDATION, RepetitionStatus.FAILED_MEASUREMENT)
    while repetition.status in failed_states and attempt < config.max_retries:
        history.append(f"attempt {attempt + 1} failed: {repetition.validation_notes}")
        attempt += 1
        repetition.retry()
        session.add(repetition)
        session.commit()
        repetition = _execute_repetition(
            session, repetition, version, config, run_dir / f"retry_{attempt}", canonical_counts
        )
    return _record_attempt_history(session, repetition, history)


def retry_repetition(
    session: Session,
    repetition: Repetition,
    version: AppVersion,
    config: SessionConfig,
    run_dir: Path,
    canonical_counts: CanonicalCounts | None = None,
) -> Repetition:
    """Manually retry one repetition left in a terminal failed state (the CLI `retry` command).

    Distinct from `run_repetition_with_retries`'s bounded auto-retry loop: this
    fires exactly one more `retry` transition and one more execution, for a
    human who has already diagnosed and fixed whatever caused the failure, after
    the auto-retry budget was already exhausted.
    """
    history = [f"earlier attempt failed: {repetition.validation_notes}"]
    repetition.retry()
    session.add(repetition)
    session.commit()
    retried = _execute_repetition(session, repetition, version, config, run_dir, canonical_counts)
    # Numbered as "earlier" rather than by index: this entry point is reached after the auto-retry
    # budget was already spent, so its own notes already carry that history and counting again
    # here would restart from one.
    return _record_attempt_history(session, retried, history)


def _naive_utc_now() -> datetime:
    """Now, on the same naive-UTC clock the database stores (SQLite's `DateTime` drops `tzinfo`)."""
    return datetime.now(UTC).replace(tzinfo=None)


def reconcile_stale_repetitions(session: Session) -> list[Repetition]:
    """Fail every repetition still `RUNNING` from a session that died mid-repetition.

    Such a row has no way out on its own: `retry` fires only from the two `FAILED_*` states, so
    nothing in the state machine can move a `RUNNING` row left behind by a crashed session. No
    timeout has to be guessed at either, because a host runs one session at a time
    (`start_monitor` refuses a second METRION monitor) and this runs before the new session
    creates any row of its own: a repetition found `RUNNING` here provably is not running.

    Bookkeeping and provenance only. Evaluation excludes every non-`ok` repetition already, so no
    reported number moves; what changes is that an abandoned repetition says so rather than
    looking live.
    """
    stale = session.query(Repetition).filter(Repetition.status == RepetitionStatus.RUNNING).all()
    for repetition in stale:
        started_at = repetition.measurement_started_at
        if started_at is None:
            age = "at an unrecorded time, since measurement_started_at is not set"
        else:
            if started_at.tzinfo is not None:
                started_at = started_at.astimezone(UTC).replace(tzinfo=None)
            age = f"{(_naive_utc_now() - started_at).total_seconds():.0f} s before this session"
        logger.warning(
            f"repetition {repetition.id} was still RUNNING from an earlier session, "
            f"failing it as abandoned (measurement began {age})"
        )
        _fail_measurement(
            session,
            repetition,
            f"session abandoned: still RUNNING when a new session started, measurement began {age}",
        )
    return stale


def run_session(
    session: Session,
    scenario: Scenario,
    version_a: AppVersion,
    version_b: AppVersion,
    repetitions_per_version: int,
    config: SessionConfig,
    seed: int | None = None,
) -> ComparisonSession:
    """Run one comparison session end to end.

    Fails abandoned repetitions first (`reconcile_stale_repetitions`), even if this session
    then halts. Raises `OrchestratorError` if the session halts before measuring: a `FLAGGED`
    equivalence verdict, a `LifecycleError` or `MetrionBridgeError` during the pre-flight dry
    runs, or a `MetrionBridgeError` during calibration. Once the repetition loop starts, these
    errors fail only the repetition concerned and the session continues.
    """
    reconcile_stale_repetitions(session)
    try:
        equivalence_check = run_preflight_gate(session, scenario, version_a, version_b, config)
    except (LifecycleError, MetrionBridgeError) as exc:
        raise OrchestratorError(f"pre-flight dry run failed: {exc}") from exc
    if equivalence_check.verdict not in (
        EquivalenceVerdict.PASS,
        EquivalenceVerdict.PASS_OVERRIDDEN,
    ):
        raise OrchestratorError(
            f"pre-flight equivalence_check {equivalence_check.id} is "
            f"{equivalence_check.verdict.value}, halting before any real measurement"
        )

    try:
        comparison_session = start_comparison_session(
            session, scenario, version_a, version_b, config, repetitions_per_version, seed=seed
        )
    except MetrionBridgeError as exc:
        raise OrchestratorError(f"session settle/calibration failed: {exc}") from exc
    versions_by_id = {version_a.id: version_a, version_b.id: version_b}
    version_runs_by_id = {vr.app_version_id: vr for vr in comparison_session.version_runs}
    canonical_counts_by_id = {
        version_a.id: CanonicalCounts(
            equivalence_check.step_count_a,
            equivalence_check.action_count_a,
            equivalence_check.entity_count_a,
        ),
        version_b.id: CanonicalCounts(
            equivalence_check.step_count_b,
            equivalence_check.action_count_b,
            equivalence_check.entity_count_b,
        ),
    }

    repetition_indices = {version_a.id: 0, version_b.id: 0}
    for slot, version_id in enumerate(comparison_session.execution_order):
        version_run = version_runs_by_id[version_id]
        if version_run.started_at is None:
            version_run.started_at = datetime.now(UTC)

        run_dir = config.data_dir / str(comparison_session.id) / f"slot_{slot:03d}"
        run_repetition_with_retries(
            session,
            version_run,
            versions_by_id[version_id],
            repetition_indices[version_id],
            config,
            run_dir,
            canonical_counts=canonical_counts_by_id[version_id],
        )
        repetition_indices[version_id] += 1
        if repetition_indices[version_id] == repetitions_per_version:
            version_run.ended_at = datetime.now(UTC)
        session.add(version_run)
        session.commit()

    return comparison_session
