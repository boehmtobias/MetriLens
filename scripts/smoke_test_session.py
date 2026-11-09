"""Manual end-to-end smoke test: one small real comparison session.

Separate from the pre-flight dry run inside every session. Runs reset, readiness, METRION
and the writer for a few repetitions, after a pipeline change or on a new machine, and first
checks that every `sudo` call the pipeline makes is passwordless.

Reads the two `app_version` rows it is given (loaded with `metrilens app-version load`) and
never creates rows, so it exercises exactly the stacks a real session would use.
"""

import hashlib
import socket
import stat
import sys
from pathlib import Path
from subprocess import run

import click
from loguru import logger

from metrilens.db import get_engine, get_session
from metrilens.metrion_integration import (
    _SYSTEM_PYTHON,
    DEFAULT_ANALYZE_WRAPPER_PATH,
    DEFAULT_BATCH_CLEANUP_SCRIPT_PATH,
    _metrion_interpreter_pattern,
    find_metrion_pids,
    monitor_command,
)
from metrilens.models import AppVersion, RepetitionStatus
from metrilens.orchestrator import (
    OrchestratorError,
    SessionConfig,
    load_session_config,
    run_session,
)


def _sudo_permitted(*command: str) -> bool:
    """Check a command's `sudo` permission without running it (`sudo -l` reports whether the
    security policy allows it and exits nonzero if not, it never executes the command)."""
    check = run(["sudo", "-n", "-l", *command], capture_output=True, text=True, timeout=10)
    return check.returncode == 0


def _is_group_or_other_writable(path: Path) -> bool:
    """Whether anyone but the owner can write `path`. Symlink modes are meaningless, so resolve."""
    mode = path.resolve().stat().st_mode
    return bool(mode & (stat.S_IWGRP | stat.S_IWOTH))


def check_root_owned_copies(config: SessionConfig) -> list[str]:
    """Confirm every root-owned copy of a checked-in script still matches the checkout.

    Returns problem labels in the same shape as `check_passwordless_sudo`, so both can be
    reported together by the same callers.

    Two scripts are deliberately installed twice: the batch-file cleanup and the analyze
    wrapper. Both are executed by root through a sudoers grant while the checkout may be
    group-writable by a human account, so root must run a copy that account cannot rewrite.
    Nothing keeps the pairs in sync, and a stale copy is silent: the pipeline keeps working
    and root keeps running older validation logic. So the agreement is checked rather than
    assumed.

    Three assertions per pair, and the third is the one a file-mode check alone would miss:
    write access to the *directory* replaces the file whatever the file's own mode says.

    A pair whose configured path is still the in-checkout default is skipped, since there is
    no second copy to disagree with.
    """
    pairs = (
        (
            "batch cleanup script",
            config.batch_cleanup_script_path,
            DEFAULT_BATCH_CLEANUP_SCRIPT_PATH,
        ),
        ("analyze wrapper", config.analyze_wrapper_path, DEFAULT_ANALYZE_WRAPPER_PATH),
    )
    problems: list[str] = []
    for label, configured, default in pairs:
        problems.extend(_check_one_root_owned_copy(label, configured.resolve(), default.resolve()))
    return problems


def _check_one_root_owned_copy(label: str, installed: Path, source: Path) -> list[str]:
    if installed == source:
        return []
    if not source.is_file():
        return [f"{label}: in-checkout copy missing, cannot verify {installed} against {source}"]

    problems: list[str] = []
    installed_digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if installed_digest != source_digest:
        problems.append(
            f"{label}: installed copy is stale, {installed} ({installed_digest[:12]}) "
            f"differs from {source} ({source_digest[:12]}); re-copy it"
        )

    if installed.stat().st_uid != 0:
        problems.append(f"{label}: installed copy is not owned by root: {installed}")

    targets = ((installed, "installed copy"), (installed.parent, "its directory"))
    for path, what in targets:
        if _is_group_or_other_writable(path):
            problems.append(
                f"{label}: {what} is group- or other-writable, "
                f"so root runs replaceable code: {path}"
            )

    return problems


def check_passwordless_sudo(config: SessionConfig) -> list[str]:
    """Labels of the pipeline's `sudo` calls that would prompt for a password.

    A missing NOPASSWD grant otherwise surfaces only well into a run, as a hang on a prompt
    nobody can answer. Covers every `sudo` call site in `metrion_integration.py`: `metrion
    monitor`, the analyze wrapper, `stop_monitor`'s `pkill` signalling, the idle-log deletion
    and the batch-file cleanup script.
    """
    # Imported from the call site rather than recomputed, so this check cannot drift from what
    # `stop_monitor` actually signals. One pattern for every `-f` call the pipeline makes, matching
    # `stop_monitor`'s own choice: matching on the launched argv is a grant sudo-rs cannot express,
    # so both sides use the interpreter pattern instead.
    signal_pattern = _metrion_interpreter_pattern(config.metrion_binary_path)
    checks: dict[str, list[str]] = {
        # The full `monitor` argv, not the bare binary: an exact-command-pattern sudoers grant can
        # match the binary while rejecting the argv the pipeline actually builds, or vice versa if
        # the grant names a config path the check doesn't include. Every entry here is the argv a
        # call site actually builds. Values that genuinely vary are stand-ins, since `sudo -l` only
        # matches the pattern and never executes anything.
        f"metrion monitor ({config.metrion_binary_path})": monitor_command(
            config.metrion_binary_path, config.metrion_config_path
        ),
        # The analyze grant names the root-owned wrapper rather than METRION's own argv, because
        # sudo-rs rejects wildcards in command arguments and three of those values vary per
        # repetition. The stand-in database sits under the *configured* `metrion_db_dir` rather
        # than a `/nonexistent/` path: `sudo -l` only pattern-matches and never executes, so the
        # file need not exist, but the path has to look like the real one for the check to mean
        # anything about the real call.
        f"metrion analyze wrapper ({config.analyze_wrapper_path})": [
            _SYSTEM_PYTHON,
            str(config.analyze_wrapper_path),
            str(config.metrion_db_dir / "sudo-check.db"),
            "2026-01-01 00:00:00",
            "2026-01-01 00:00:01",
        ],
        # `stop_monitor` graceful, `_kill_orphaned_metrion_workers`, and both timeout paths, all on
        # the one pattern. No `pgrep` entry of any kind: every PID lookup in the pipeline reads
        # `/proc` with an unprivileged `pgrep`, so a grant for it would be unused, and an unused
        # grant risks masking a genuinely missing one elsewhere.
        f"pkill -INT -f (stop_monitor, {signal_pattern})": ["pkill", "-INT", "-f", signal_pattern],
        f"pkill -KILL -f (kill paths, {signal_pattern})": [
            "pkill",
            "-KILL",
            "-f",
            signal_pattern,
        ],
        # The `-P <pid>` child sweep. `_signal_metrion_tree` sends both signals this way on every
        # stop, and the pid cannot be constrained in a sudoers pattern, so the grant wildcards it
        # and the stand-in below is arbitrary.
        "pkill -INT -P (child sweep)": ["pkill", "-INT", "-P", "12345"],
        "pkill -KILL -P (child sweep)": ["pkill", "-KILL", "-P", "12345"],
        f"idle log rm ({config.metrion_idle_log_path})": [
            "rm",
            "-f",
            str(config.metrion_idle_log_path),
        ],
        # The configured script, not the in-repo default: on a machine whose checkout is shared
        # with a human account the grant names a root-owned copy elsewhere, and checking the
        # default would pass while every real cleanup call prompted for a password.
        f"batch file cleanup ({config.batch_cleanup_script_path})": [
            _SYSTEM_PYTHON,
            str(config.batch_cleanup_script_path),
            "/tmp/batch_sudo_check_dummy.ipc",
        ],
    }
    return [label for label, command in checks.items() if not _sudo_permitted(*command)]


@click.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default="config.yaml",
    show_default=True,
    help="MetriLens's own YAML config.",
)
@click.option(
    "--version-a-id",
    type=int,
    required=True,
    help="Baseline app version to smoke-test, already loaded via `metrilens app-version load`.",
)
@click.option(
    "--version-b-id",
    type=int,
    required=True,
    help="App version compared against the baseline; must share version A's scenario.",
)
@click.option("--repetitions", type=int, default=1, show_default=True)
@click.option("--data-dir", type=click.Path(path_type=Path), default="data/raw", show_default=True)
@click.option("--seed", type=int, default=None, help="Fix execution_order's random seed.")
def main(
    config_path: Path,
    version_a_id: int,
    version_b_id: int,
    repetitions: int,
    data_dir: Path,
    seed: int | None,
) -> None:
    """Run one small real session end to end and report whether every repetition succeeded."""
    session = get_session(get_engine())
    with session:
        version_a = session.get(AppVersion, version_a_id)
        version_b = session.get(AppVersion, version_b_id)
        missing = [
            version_id
            for version_id, version in ((version_a_id, version_a), (version_b_id, version_b))
            if version is None
        ]
        if missing:
            logger.error(
                "no app_version with id={}; run `metrilens app-version load` first",
                "/".join(str(version_id) for version_id in missing),
            )
            sys.exit(1)
        if version_a.scenario_id != version_b.scenario_id:
            logger.error(
                "app_version {} and {} belong to different scenarios ({} and {}); "
                "a session compares two versions of one scenario",
                version_a_id,
                version_b_id,
                version_a.scenario_id,
                version_b.scenario_id,
            )
            sys.exit(1)

        scenario = version_a.scenario
        logger.info(
            "smoke-testing scenario '{}' (id={}) with versions '{}' (id={}) / '{}' (id={})",
            scenario.name,
            scenario.id,
            version_a.label,
            version_a.id,
            version_b.label,
            version_b.id,
        )

        config = load_session_config(config_path, data_dir, socket.gethostname())
        sudo_problems = check_passwordless_sudo(config)
        if sudo_problems:
            logger.error(
                "sudo would prompt for a password on this machine for: {}; "
                "add NOPASSWD sudoers entries for these before running a real session",
                ", ".join(sudo_problems),
            )
            sys.exit(1)

        try:
            comparison_session = run_session(
                session, scenario, version_a, version_b, repetitions, config, seed=seed
            )
        except OrchestratorError as exc:
            logger.error("session halted: {}", exc)
            sys.exit(1)

        logger.info(
            "comparison_session id={} execution_order={}",
            comparison_session.id,
            comparison_session.execution_order,
        )

        failures = []
        for version_run in comparison_session.version_runs:
            for repetition in version_run.repetitions:
                if repetition.status is RepetitionStatus.OK:
                    logger.success(
                        "repetition {} (version_run {}): OK, cpu_energy_j={}, runtime_s={}",
                        repetition.repetition_index,
                        version_run.id,
                        repetition.cpu_energy_j,
                        repetition.runtime_s,
                    )
                else:
                    failures.append(repetition)
                    logger.error(
                        "repetition {} (version_run {}): {} - {}",
                        repetition.repetition_index,
                        version_run.id,
                        repetition.status.value,
                        repetition.validation_notes,
                    )

        orphaned_pids = find_metrion_pids(config.metrion_binary_path)
        if orphaned_pids:
            logger.error(
                "{} orphaned METRION process(es) still running after the session (pids {}); "
                "the orchestrator's orphan sweep should have caught this",
                len(orphaned_pids),
                orphaned_pids,
            )

        if failures or orphaned_pids:
            if failures:
                logger.error("{} repetition(s) did not reach OK", len(failures))
            sys.exit(1)
        logger.success("smoke test passed: every repetition reached OK, no orphaned processes")


if __name__ == "__main__":
    main()
