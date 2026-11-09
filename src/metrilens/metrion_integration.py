"""Bridge to METRION's CLI.

Runs `metrion monitor` and `analyze` as subprocesses, resolves the target app's containers,
and forces a fresh idle-power calibration. MetriLens never imports `metrion` as a library:
every touchpoint is a subprocess call or, for the idle log, a file deleted to force
regeneration.

`analyze` exports the whole host unfiltered; the target app's rows are selected afterwards by
cgroup (`attribution.py`), so no PID list bounds the measurement.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from subprocess import STDOUT, CompletedProcess, Popen, TimeoutExpired, run

import yaml
from loguru import logger


class MetrionBridgeError(RuntimeError):
    """Raised when a configured METRION path is missing, or a METRION call fails."""


class MonitorKilledError(MetrionBridgeError):
    """`metrion monitor` had to be killed because it did not exit within its grace period.

    A separate subclass because a kill does not imply lost data: METRION's shutdown can be slow
    rather than hung, so the database usually still holds the whole window, and the caller
    checks export coverage instead of discarding the repetition. Every other `stop_monitor`
    failure stays a plain `MetrionBridgeError`.
    """


def _load_config(config_path: Path) -> dict:
    return yaml.safe_load(config_path.read_text()) or {}


def load_metrion_binary_path(config_path: Path) -> Path:
    """Read `metrion_binary_path` from MetriLens's own YAML config.

    The binary lives in METRION's own separate virtual environment; this is
    the one configured value that bridges the two, kept out of MetriLens's own
    dependencies rather than importing METRION as a library.
    """
    raw_path = _load_config(config_path).get("metrion_binary_path")
    if not raw_path:
        raise MetrionBridgeError(f"'metrion_binary_path' missing from {config_path}")
    binary_path = Path(raw_path).expanduser()
    if not binary_path.is_file():
        raise MetrionBridgeError(f"configured metrion binary not found: {binary_path}")
    return binary_path


def load_metrion_output_dir(config_path: Path) -> Path:
    """Read `metrion_output_dir` from MetriLens's own YAML config.

    `metrion analyze --export-*` always writes into a fixed `output/` directory
    inside METRION's own installation, not somewhere MetriLens's CLI call can
    redirect; MetriLens needs this path to archive exports out of it.
    """
    raw_path = _load_config(config_path).get("metrion_output_dir")
    if not raw_path:
        raise MetrionBridgeError(f"'metrion_output_dir' missing from {config_path}")
    output_dir = Path(raw_path).expanduser()
    if not output_dir.is_dir():
        raise MetrionBridgeError(f"configured metrion output dir not found: {output_dir}")
    return output_dir


def load_metrion_idle_log_path(config_path: Path) -> Path:
    """Read `metrion_idle_log_path` from MetriLens's own YAML config.

    Points at METRION's own cached RAPL idle-calibration log; deleting this file
    is how MetriLens forces a fresh calibration through METRION's CLI rather than
    reaching into its internals. Not required to exist yet, only to be configured.
    """
    raw_path = _load_config(config_path).get("metrion_idle_log_path")
    if not raw_path:
        raise MetrionBridgeError(f"'metrion_idle_log_path' missing from {config_path}")
    return Path(raw_path).expanduser()


def load_metrion_config_path(config_path: Path) -> Path:
    """Read `metrion_config_path` from MetriLens's own YAML config.

    METRION's own `config.yaml` (passed as `-c <path>` to `metrion monitor`),
    distinct from this function's own `config_path` argument, which is
    MetriLens's config. Also the value recorded on `comparison_session.
    metrion_config_path`, so a reviewer can see which METRION configuration a
    session's runs used.
    """
    raw_path = _load_config(config_path).get("metrion_config_path")
    if not raw_path:
        raise MetrionBridgeError(f"'metrion_config_path' missing from {config_path}")
    metrion_config_path = Path(raw_path).expanduser()
    if not metrion_config_path.is_file():
        raise MetrionBridgeError(f"configured METRION config not found: {metrion_config_path}")
    return metrion_config_path


def load_metrion_db_dir(config_path: Path) -> Path:
    """Read `metrion_db_dir` from MetriLens's own YAML config.

    The directory `metrion monitor` writes its own per-run database into
    (METRION's own `general.database.path`, resolved relative to its install
    root). Deliberately a directory, not a fixed filename: with METRION's own
    `debug: true`, it timestamps that filename on every run to avoid overwriting
    the previous one, so a fixed path can't be assumed either way;
    `find_new_db_file` locates the right one after each `monitor` run.
    """
    raw_path = _load_config(config_path).get("metrion_db_dir")
    if not raw_path:
        raise MetrionBridgeError(f"'metrion_db_dir' missing from {config_path}")
    db_dir = Path(raw_path).expanduser()
    if not db_dir.is_dir():
        raise MetrionBridgeError(f"configured metrion db dir not found: {db_dir}")
    return db_dir


# Slack on the mtime comparison in `find_new_db_file`, for filesystems that store coarser
# timestamps than the clock the cutoff is read from. The case it guards against is a database
# written moments after the cutoff whose recorded mtime rounds down below it; the case it must not
# admit is a database from a previous session, which is hours or days away, so a second is ample.
_MTIME_TOLERANCE_S = 1.0


def find_new_db_file(db_dir: Path, before: set[Path], *, not_before: float) -> Path:
    """The database file written by the most recent `metrion monitor` run.

    Compares `db_dir`'s `*.db` files with the `before` snapshot. A new file wins (METRION with
    `debug: true` timestamps each database); otherwise the newest file modified since
    `not_before` wins (a fixed filename overwritten in place).

    `not_before`, the wall clock just before the monitor started, keeps the fallback from
    returning an older, unrelated database when METRION writes somewhere other than `db_dir`;
    that case raises instead.
    """
    after = set(db_dir.glob("*.db"))
    appeared = after - before
    candidates = appeared or {
        path for path in after if path.stat().st_mtime + _MTIME_TOLERANCE_S >= not_before
    }
    if not candidates:
        raise MetrionBridgeError(
            f"no .db file in {db_dir} was written by this 'metrion monitor' run "
            f"({len(after)} database(s) present, none newer than the monitor's own start). "
            "Most likely METRION's own 'general.database.path' and MetriLens's 'metrion_db_dir' "
            "no longer point at the same directory."
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def confirm_metrion_bridge(config_path: Path) -> str:
    """Run a trivial `metrion --help` call through the configured binary path.

    Confirms MetriLens can invoke METRION's separate venv from its own, with
    neither venv activated.
    """
    binary_path = load_metrion_binary_path(config_path)
    result = run([str(binary_path), "--help"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise MetrionBridgeError(
            f"'{binary_path} --help' exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def resolve_compose_pids(compose_file: Path) -> list[int]:
    """Host PIDs of every process running inside the target app's Compose project.

    Uses `docker top` on every container, which lists all its processes, rather than each
    container's PID 1 alone.

    These PIDs do not bound the measurement (rows are selected by cgroup, see
    `attribution.py`). The call fails early if the stack is not up before the monitor starts,
    and the list is logged with the measured window for diagnosis.
    """
    ps = run(
        ["docker", "compose", "-f", str(compose_file), "ps", "-q"],
        cwd=compose_file.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if ps.returncode != 0:
        raise MetrionBridgeError(f"'docker compose ps -q' failed: {ps.stderr.strip()}")
    container_ids = [line.strip() for line in ps.stdout.splitlines() if line.strip()]
    if not container_ids:
        raise MetrionBridgeError(
            f"no running containers found for compose project at {compose_file}"
        )

    pids = []
    for container_id in container_ids:
        top = run(
            ["docker", "top", container_id],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if top.returncode != 0:
            raise MetrionBridgeError(f"'docker top {container_id}' failed: {top.stderr.strip()}")
        lines = top.stdout.splitlines()[1:]  # skip the "UID PID PPID ..." header
        pids.extend(int(line.split()[1]) for line in lines if line.strip())
    return pids


def resolve_compose_container_map(compose_file: Path) -> dict[str, str]:
    """Docker's 12-char short container ID -> Compose service name, captured at
    collection time so a per-container energy breakdown can later attribute the raw
    per-thread export's `resource_group_name` (a cgroup path embedding the full,
    64-char container ID, which always starts with this same 12-char short ID) back
    to a role like `inventree-server`, even once that specific container is long
    torn down by the next repetition's reset and can no longer be queried directly.
    """
    ps = run(
        ["docker", "compose", "-f", str(compose_file), "ps", "--format", "json"],
        cwd=compose_file.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if ps.returncode != 0:
        raise MetrionBridgeError(f"'docker compose ps --format json' failed: {ps.stderr.strip()}")

    container_map = {}
    for line in ps.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        container = json.loads(line)
        container_map[container["ID"]] = container["Service"]
    return container_map


def _metrion_interpreter_pattern(binary_path: Path) -> str:
    """`pgrep -f` pattern matching METRION's own venv interpreter, main process or worker.

    Anchored to `binary_path`'s own venv (`<venv>/bin/python3`), not a generic
    "multiprocessing" substring: that string is standard CPython boilerplate
    shared by every multiprocessing-based Python program, so a generic match
    would risk killing another tenant's unrelated process on a shared machine.
    Every orphaned METRION worker invokes this exact interpreter path as
    `argv[0]`, and so does `metrion` itself once its own shebang correctly
    resolves into its venv, so one pattern catches both.
    """
    return str(binary_path.parent / "python3")


def find_metrion_pids(binary_path: Path) -> list[int]:
    """PIDs of any process invoking METRION's own venv interpreter, main or orphaned.

    Plain `pgrep`, no `sudo`: reading another process's command line via `/proc`
    doesn't require root, only signaling one does.
    """
    result = run(
        ["pgrep", "-f", _metrion_interpreter_pattern(binary_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return [int(pid) for pid in result.stdout.split()]


_BATCH_FILE_DIR = Path("/tmp")
_BATCH_FILE_PATTERN = "batch_*.ipc"
# The system interpreter the root-owned helper scripts run under. Named generally rather than
# per-script since more than one grant uses it, and deliberately not METRION's own venv
# interpreter: these scripts are stdlib-only precisely so they need none.
_SYSTEM_PYTHON = "/usr/bin/python3"
# Where the trusted cleanup script lives when the config does not say otherwise: inside this
# checkout. That default is only safe while the checkout is writable by exactly the account
# holding the sudoers grant. It is not safe on a machine where the work tree is shared with a
# human account, because root would then be running a script a non-root account can rewrite.
# Such a machine sets `batch_cleanup_script_path` to a root-owned copy instead.
DEFAULT_BATCH_CLEANUP_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "cleanup_orphan_batch_files.py"
)

# Same story for the analyze wrapper: the in-checkout copy is the default, and a machine whose
# work tree is shared with a human account points this at a root-owned copy.
DEFAULT_ANALYZE_WRAPPER_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "metrion_analyze_wrapper.py"
)


def snapshot_batch_files() -> set[Path]:
    """Snapshot METRION's `/tmp/batch_*.ipc` files, before a monitor process starts.

    Callers pass this to `stop_monitor`/`_kill_orphaned_metrion_workers` so cleanup can
    diff against it later, deleting only files that appeared during our own tracked
    process's lifetime. `/tmp` can be shared by other users on a multi-tenant machine and
    METRION always writes these `root:root` regardless of whose `sudo` triggered it, so
    ownership alone can't tell "ours" from a concurrent tenant's run; this before/after
    diff (mirroring `find_new_db_file`'s existing pattern) is the only safe proxy.
    """
    return set(_BATCH_FILE_DIR.glob(_BATCH_FILE_PATTERN))


def load_batch_cleanup_script_path(config_path: Path) -> Path:
    """Read the optional `batch_cleanup_script_path` from MetriLens's own YAML config.

    Optional, unlike the METRION paths above: absent means the in-checkout script. Set it to
    a root-owned copy wherever the checkout is not owned solely by the account the sudoers
    grant is written for.
    """
    raw_path = _load_config(config_path).get("batch_cleanup_script_path")
    if not raw_path:
        return DEFAULT_BATCH_CLEANUP_SCRIPT_PATH
    script_path = Path(raw_path).expanduser()
    if not script_path.is_file():
        raise MetrionBridgeError(f"configured batch cleanup script not found: {script_path}")
    return script_path


def load_analyze_wrapper_path(config_path: Path) -> Path:
    """Read the optional `analyze_wrapper_path` from MetriLens's own YAML config.

    Optional for the same reason `batch_cleanup_script_path` is: absent means the in-checkout
    script, which is correct wherever the checkout is writable only by the account holding the
    sudoers grant.
    """
    raw_path = _load_config(config_path).get("analyze_wrapper_path")
    if not raw_path:
        return DEFAULT_ANALYZE_WRAPPER_PATH
    wrapper_path = Path(raw_path).expanduser()
    if not wrapper_path.is_file():
        raise MetrionBridgeError(f"configured analyze wrapper not found: {wrapper_path}")
    return wrapper_path


def _cleanup_new_batch_files(files_before: set[Path], cleanup_script_path: Path) -> None:
    """Delete only the batch files that appeared since `files_before` was snapshotted.

    Deliberately does not `rm` by a wildcard sudoers grant directly: the exact set of
    paths is computed here, unprivileged, and handed to a small trusted script
    (`scripts/cleanup_orphan_batch_files.py`) that re-validates each path itself
    (must be `/tmp/batch_*.ipc`, not a symlink) before deleting, so even a
    compromised/buggy caller here can't turn this into an arbitrary-file delete.
    Best-effort: a failure here never raises, since a leftover file is a disk-space
    concern, not a measurement-correctness one.
    """
    new_files = set(_BATCH_FILE_DIR.glob(_BATCH_FILE_PATTERN)) - files_before
    if not new_files:
        return
    result = run(
        ["sudo", _SYSTEM_PYTHON, str(cleanup_script_path), *(str(p) for p in sorted(new_files))],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        logger.warning(
            "batch file cleanup reported errors ({} file(s) attempted): {}",
            len(new_files),
            result.stderr.strip(),
        )


def _kill_orphaned_metrion_workers(
    binary_path: Path,
    batch_files_before: set[Path] | None = None,
    cleanup_script_path: Path | None = None,
) -> list[int]:
    """Kill any process still invoking METRION's own venv interpreter.

    Only ever called after `stop_monitor` has already confirmed its own tracked
    process exited, so anything still matching at that point is a genuine
    orphan: METRION's `multiprocessing` workers survive `stop_monitor`'s
    `SIGINT`/`pkill` to the main process (it never propagates to them, no
    `PR_SET_PDEATHSIG`/signal-handler cleanup inside METRION itself), and
    orphaned workers left running unbounded can fill a shared disk.

    If `batch_files_before` is given, also cleans up any
    `/tmp/batch_*.ipc` files that appeared during this process's lifetime, since
    an orphaned worker's files are already written well before it's killed here;
    killing the process alone doesn't reclaim the disk space it already used.
    Runs regardless of whether any orphaned PIDs were actually found, since a
    process can finish writing its files and exit on its own before this sweep
    ever runs.
    """
    pids = find_metrion_pids(binary_path)
    if pids:
        # Via `_sudo_signal_call`, for the same reason the stop path uses it: a refused `sudo`
        # here would otherwise leave every orphan running while this logged that it had killed
        # them, which is worse than a plain failure because the log would say the opposite.
        _sudo_signal_call(["pkill", "-KILL", "-f", _metrion_interpreter_pattern(binary_path)])
        logger.warning("killed {} orphaned METRION worker process(es): {}", len(pids), pids)
    if batch_files_before is not None:
        _cleanup_new_batch_files(
            batch_files_before, cleanup_script_path or DEFAULT_BATCH_CLEANUP_SCRIPT_PATH
        )
    return pids


def monitor_command(binary_path: Path, config_path: Path) -> list[str]:
    """The argv `start_monitor` runs under `sudo`, minus the `sudo` itself.

    Exists so the launcher and the pre-flight sudo check read from one construction: the grant
    names this argv exactly, so a check built from anything else would be checking a different
    command than the one actually run.
    """
    return [str(binary_path), "monitor", "-c", str(config_path)]


MONITOR_LOG_NAME = "metrion_monitor.log"
_MONITOR_LOG_TAIL_LINES = 20


def read_monitor_log_tail(log_path: Path | None, lines: int = _MONITOR_LOG_TAIL_LINES) -> str:
    """The last lines METRION wrote, for an error message that would otherwise say nothing."""
    if log_path is None or not log_path.exists():
        return ""
    tail = log_path.read_text(errors="replace").splitlines()[-lines:]
    return "\n".join(tail)


def start_monitor(binary_path: Path, config_path: Path, log_path: Path) -> Popen:
    """Launch `metrion monitor` under `sudo` (needed for eBPF) and return without waiting.

    One fresh process and one database per repetition; stop it with `stop_monitor`. Refuses to
    start while any METRION process, main or orphaned worker, is already running, so a leaked
    one cannot go unnoticed.

    Output goes to `log_path`, not a pipe: an unread pipe can fill during METRION's startup scan
    and block it inside the measured window. The file also keeps the log for diagnosis.

    A plain `Popen` with no process-group handling, since signals from this unprivileged process
    reach only the `sudo` wrapper; `stop_monitor` uses a separate `sudo pkill` instead.
    `start_new_session=True` would also break `sudo`'s TTY password prompt.
    """
    already_running = find_metrion_pids(binary_path)
    if already_running:
        raise MetrionBridgeError(
            f"refusing to start 'metrion monitor': already running (pids {already_running})"
        )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # The parent's handle is closed on exit from the `with`; the child holds its own duplicate.
    with log_path.open("wb") as log_file:
        return Popen(
            ["sudo", *monitor_command(binary_path, config_path)],
            stdout=log_file,
            stderr=STDOUT,
        )


def _signal_metrion_tree(signal_name: str, binary_path: Path) -> None:
    """Signal METRION's processes and their direct children.

    Children are signalled first (`pkill -P`), so a parent is still there when its children
    are looked up.

    Matches on `_metrion_interpreter_pattern`, not the monitor's full command line: sudo-rs
    compares argument tokens, so a sudoers grant cannot match a multi-word pattern. This is safe
    because `start_monitor` refuses to launch while anything matches the pattern, so during a
    repetition it matches only this monitor and its workers. PID lookup is an unprivileged
    `pgrep`, so no `sudo pgrep` grant is needed.
    """
    for pid in find_metrion_pids(binary_path):
        _sudo_signal_call(["pkill", signal_name, "-P", str(pid)])
    _sudo_signal_call(["pkill", signal_name, "-f", _metrion_interpreter_pattern(binary_path)])


def _sudo_signal_call(command: list[str]) -> CompletedProcess:
    """Run one `sudo pkill` and raise if `sudo` refused it.

    Without this, a missing sudoers grant would look like a signal that was sent and ignored.

    The exit status cannot tell: `pkill` exits 1 when nothing matched, which is normal here, and
    a `sudo pkill -f` can kill its own `sudo` wrapper (whose command line contains the pattern),
    returning -9 on success. `sudo` writes to stderr when it refuses and `pkill` writes nothing,
    so stderr is the check.
    """
    result = run(["sudo", *command], capture_output=True, text=True, timeout=10)
    if result.stderr.strip():
        raise MetrionBridgeError(
            f"'sudo {' '.join(command)}' could not be run, so nothing was signalled:"
            f" {result.stderr.strip()}"
        )
    return result


# How long `metrion monitor` gets to exit after SIGINT before it is killed. An upper bound, not
# a routine wait. A killed stop is not by itself a reason to raise it: `write_repetition`'s
# coverage check decides whether the exported window is complete either way.
DEFAULT_MONITOR_STOP_TIMEOUT_S = 90.0
# After SIGKILL the process is gone at once, so the second wait only needs to reap it.
_KILL_REAP_TIMEOUT_S = 30.0


def stop_monitor(
    process: Popen,
    binary_path: Path,
    timeout_s: float = DEFAULT_MONITOR_STOP_TIMEOUT_S,
    batch_files_before: set[Path] | None = None,
    cleanup_script_path: Path | None = None,
    log_path: Path | None = None,
) -> None:
    """Stop a running `metrion monitor` with SIGINT, falling back to SIGKILL after `timeout_s`.

    METRION flushes its database only on SIGINT. The signal goes through a separate `sudo
    pkill` (see `_signal_metrion_tree`), because signalling the tracked `Popen` reaches only the
    `sudo` wrapper, which exits once METRION does. Direct children are signalled too, since
    SIGKILL does not propagate to them.

    Once the process has exited, a second sweep kills any orphaned METRION workers and, if
    `batch_files_before` (from `snapshot_batch_files`) is given, deletes the `/tmp/batch_*.ipc`
    files that appeared since.

    Raises `MonitorKilledError` if the monitor had to be killed, and `MetrionBridgeError` if it
    exited non-zero.
    """
    _signal_metrion_tree("-INT", binary_path)
    try:
        process.wait(timeout=timeout_s)
    except TimeoutExpired as exc:
        _signal_metrion_tree("-KILL", binary_path)
        process.wait(timeout=_KILL_REAP_TIMEOUT_S)
        _kill_orphaned_metrion_workers(binary_path, batch_files_before, cleanup_script_path)
        raise MonitorKilledError(
            f"'metrion monitor' (pid {process.pid}) did not exit within {timeout_s}s"
            " after SIGINT, killed"
        ) from exc
    _kill_orphaned_metrion_workers(binary_path, batch_files_before, cleanup_script_path)
    if process.returncode != 0:
        raise MetrionBridgeError(
            f"'metrion monitor' exited {process.returncode} after stop signal:"
            f" {read_monitor_log_tail(log_path)}"
        )


# `metrion analyze` insists on at least one filter (omitting both exits 2) and offers exactly two:
# `--filter-pids`, a literal `native_pid IN (...)` in SQL, and `--filter-cmdline`, a regex matched
# against `cmdline` or `executable_name`. A regex of `.` matches every row with a
# non-empty command line, so this is how MetriLens asks for an unfiltered window. Kept as a literal
# constant rather than a parameter because the sudoers grant on the measurement host matches it
# exactly: only values that legitimately vary per repetition get a wildcard.
UNFILTERED_CMDLINE_PATTERN = "."

# The fixed tail of the analyze argv, declared here even though the root-owned wrapper is what
# actually passes it. Kept in the package because `attribution.py`'s numbers depend on the export
# being unfiltered: if the wrapper's flags drifted from this, MetriLens's results would change
# meaning with no error anywhere. `tests/test_metrion_analyze_wrapper.py` asserts the two still
# agree.
EXPECTED_WRAPPER_ANALYZE_ARGS = (
    "--filter-cmdline",
    UNFILTERED_CMDLINE_PATTERN,
    "--type",
    "duckdb",
    "--export-summary",
    "--export-raw-data",
    "--no-plots",
)


def _metrion_output_tail(result: CompletedProcess, lines: int = 15) -> str:
    """METRION's own last words, for an error that would otherwise only say what did not happen.

    Without this, the `no new files` branch below would discard both captured streams, so a
    failure METRION had already explained on stderr would reach the operator as "reported success
    but wrote nothing". METRION swallows several errors internally and still exits 0, which is
    precisely the case this reporting exists for.
    """
    parts = []
    for label, stream in (("stdout", result.stdout), ("stderr", result.stderr)):
        text = (stream or "").strip()
        if text:
            parts.append(f"{label}: " + "\n".join(text.splitlines()[-lines:]))
    return "; ".join(parts) or "(METRION wrote nothing to stdout or stderr)"


def run_analyze(
    wrapper_path: Path,
    db_path: Path,
    start_time: datetime,
    end_time: datetime,
    metrion_output_dir: Path,
    archive_dir: Path,
    timeout_s: float = 300,
) -> dict[str, Path]:
    """Run `metrion analyze` for one repetition's measured window, then archive its exports.

    METRION always writes `--export-*` output into its own fixed installation
    directory with timestamped filenames, not somewhere the CLI call can
    redirect; a second repetition's analyze call would otherwise collide with
    or bury the first's files there. This diffs that directory's contents
    before/after the call and moves whatever's new into `archive_dir`, so
    MetriLens owns its own copy rather than depending on METRION's own
    output directory surviving across runs.

    Asked for the whole window rather than for a PID list, via `--filter-cmdline "."`; the caller
    selects the target app's own rows from the per-thread export afterwards (`attribution.py`).
    METRION requires at least one filter, so the flag cannot simply be dropped.

    Does not invoke `metrion` directly: it invokes a root-owned wrapper that validates the three
    varying values and then `execv`s METRION itself with the fixed argv above. sudo-rs rejects
    wildcards in command arguments, so a grant naming this argv is inexpressible, while one naming
    the wrapper with a trailing wildcard is. Because the wrapper `execv`s rather than spawning,
    METRION's own stdout, stderr and exit code still arrive here unchanged and the handling below
    is unaffected.

    Runs via `sudo`: `metrion monitor` runs as root, so the `.db` file it produces is root-owned;
    `metrion analyze` opens it read-write (not read-only) and, without sudo, that open fails with
    a permission error that METRION itself only logs and swallows, still exiting 0 with nothing
    written. The `not new_files` check below is what actually catches that case, but running as
    root avoids hitting it at all.
    """
    before = set(metrion_output_dir.glob("*")) if metrion_output_dir.is_dir() else set()

    result = run(
        [
            "sudo",
            _SYSTEM_PYTHON,
            str(wrapper_path),
            str(db_path),
            start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time.strftime("%Y-%m-%d %H:%M:%S"),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        raise MetrionBridgeError(
            f"'metrion analyze' exited {result.returncode}: {_metrion_output_tail(result)}"
        )

    new_files = set(metrion_output_dir.glob("*")) - before
    if not new_files:
        raise MetrionBridgeError(
            f"'metrion analyze' reported success but wrote no new files to"
            f" {metrion_output_dir}: {_metrion_output_tail(result)}"
        )

    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = {}
    for path in new_files:
        destination = archive_dir / path.name
        path.rename(destination)
        archived[path.name] = destination
    return archived


_IDLE_LOG_COMPLETE_MARKER = "Package Total"


# The calibration monitor's own stop gets a much shorter grace period than a repetition's, and
# for a plain reason: nothing ever reads the database it wrote. Its only product is the idle log,
# which is already complete by the time this stops it, so every second spent waiting for a clean
# exit buys tidiness alone. 60 s leaves room for a clean exit while capping the waste on a host
# whose METRION shutdown is slow or stuck.
DEFAULT_CALIBRATION_STOP_TIMEOUT_S = 60.0


def force_idle_calibration(
    binary_path: Path,
    config_path: Path,
    idle_log_path: Path,
    monitor_log_path: Path,
    poll_timeout_s: float = 300,
    poll_interval_s: float = 2.0,
    cleanup_script_path: Path | None = None,
    stop_timeout_s: float = DEFAULT_CALIBRATION_STOP_TIMEOUT_S,
) -> Path:
    """Force a fresh RAPL idle-power calibration and return the regenerated log's path.

    Run once per session, after the pre-flight dry runs settle and before the first measured
    repetition. METRION regenerates its cached `idle_log_path` only when the file is missing,
    so this deletes it (with `sudo rm -f`, since the file is root-owned) and runs a short
    `metrion monitor`. The orchestrator parses the result.

    Completion is detected by polling the log for its `Package Total` line. `monitor --timeout`
    is not relied on because the process can hang past it, and file-size stability is not
    either because METRION's cgroup-freeze step can stall the log for longer than any useful
    stability window.
    """
    if idle_log_path.exists():
        delete = run(
            ["sudo", "rm", "-f", str(idle_log_path)], capture_output=True, text=True, timeout=10
        )
        if delete.returncode != 0:
            raise MetrionBridgeError(
                f"'sudo rm -f {idle_log_path}' failed: {delete.stderr.strip()}"
            )

    batch_files_before = snapshot_batch_files()
    process = start_monitor(binary_path, config_path, monitor_log_path)

    def _calibration_complete() -> bool:
        return idle_log_path.exists() and _IDLE_LOG_COMPLETE_MARKER in idle_log_path.read_text()

    def _accept_calibration() -> Path:
        try:
            stop_monitor(
                process,
                binary_path,
                timeout_s=stop_timeout_s,
                batch_files_before=batch_files_before,
                cleanup_script_path=cleanup_script_path,
                log_path=monitor_log_path,
            )
        except MonitorKilledError as exc:
            # Tolerated here for a simpler reason than a repetition's kill tolerance: the
            # calibration's product is the idle log, this branch only runs because that log is
            # already complete, and the process is dead by the time this is raised. Aborting the
            # session over it would throw away a completed calibration over a monitor nobody was
            # going to read anyway.
            logger.warning("{}; the calibration it was running had already completed", exc)
        return idle_log_path

    deadline = time.monotonic() + poll_timeout_s
    while time.monotonic() < deadline:
        # `start_monitor` never checks whether `sudo` itself succeeded (e.g. an auth failure)
        # before returning; without this check that failure would otherwise be indistinguishable
        # from a slow-but-legitimate calibration until the full `poll_timeout_s` had elapsed.
        if process.poll() is not None:
            raise MetrionBridgeError(
                f"calibration 'metrion monitor' exited early with code {process.returncode}"
                f" before regenerating {idle_log_path}:"
                f" {read_monitor_log_tail(monitor_log_path)}"
            )
        if _calibration_complete():
            return _accept_calibration()
        time.sleep(poll_interval_s)

    # The deadline expiring is not evidence that the calibration failed, because this loop is
    # not guaranteed to have run at full speed throughout. METRION's calibration freezes
    # `user.slice`, and MetriLens itself can sit under it, so for the duration of the freeze this
    # process is not scheduled at all while `time.monotonic()` keeps advancing regardless. The
    # deadline can therefore expire with the calibration long since finished and its marker
    # sitting unread in the log. So the marker gets the last word, since it is the actual success
    # criterion and the deadline is only ever a proxy for it.
    if _calibration_complete():
        logger.warning(
            "calibration deadline of {}s expired, but {} holds a complete calibration;"
            " accepting it. The most likely cause is this process having been frozen by"
            " METRION's own calibration freeze while polling.",
            poll_timeout_s,
            idle_log_path,
        )
        return _accept_calibration()

    # A plain `process.kill()` here would only reach the `sudo` wrapper, not the actual
    # escalated `metrion` process (the same reason `stop_monitor`'s `_signal_metrion_tree`
    # exists). Same two-step cleanup as `stop_monitor`'s timeout path.
    _signal_metrion_tree("-KILL", binary_path)
    _kill_orphaned_metrion_workers(binary_path, batch_files_before, cleanup_script_path)
    process.wait(timeout=10)
    # Say what was actually observed rather than asserting a regeneration failure, so a
    # misdiagnosis doesn't send the next investigation looking in the wrong place.
    if not idle_log_path.exists():
        observed = "the log does not exist"
    else:
        observed = (
            f"the log exists ({idle_log_path.stat().st_size} bytes) but holds no"
            f" {_IDLE_LOG_COMPLETE_MARKER!r} line"
        )
    raise MetrionBridgeError(
        f"calibration did not complete within {poll_timeout_s}s: {observed}."
        f" Monitor tail: {read_monitor_log_tail(monitor_log_path)}"
    )
