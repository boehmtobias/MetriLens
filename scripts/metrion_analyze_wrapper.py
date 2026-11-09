#!/usr/bin/env python3
"""Run `metrion analyze` for one repetition's window, invoked via `sudo`.

Exists because some sudo implementations (e.g. sudo-rs) reject wildcards in command
arguments outright, and the argv `run_analyze` builds needs three values that vary per
repetition (the database path and the window bounds). A grant naming that argv is
therefore inexpressible; a grant naming this script with a trailing wildcard is not.
As with the batch-file cleanup script, the safety property lives in auditable code here,
not in a sudoers pattern.

Everything the sudoers grant cannot constrain, this script constrains itself:

- The METRION binary and the database root are hardcoded, never taken from argv.
  That is the load-bearing property. The grant is `<this script> *`, so every argument
  is attacker-chosen from the measurement account's point of view, and a binary path
  read from argv would turn the grant into an arbitrary-root-exec primitive, strictly
  worse than the wildcard it replaces. Hardcoding the database root also means this
  root-run code never trusts a config file the measurement account can write.
- The database path must resolve inside that root, be a regular file, and not be a
  symlink (a symlink there could otherwise point anywhere).
- Both timestamps must match METRION's own `%Y-%m-%d %H:%M:%S`, so neither can smuggle
  a further argument through.

`os.execv` rather than a subprocess: it replaces the process image, so METRION's
stdout, stderr and exit code reach `run_analyze` unchanged and its existing error
handling keeps working without this script having to relay any of it.

No dependencies beyond the standard library, so it runs under the system `python3`
rather than needing METRION's or MetriLens's venv. Note that it therefore cannot
import MetriLens's own constants; `tests/test_metrion_analyze_wrapper.py` asserts that
FIXED_ANALYZE_ARGS still equals what `metrion_integration` declares, so the two flag
sets cannot silently drift apart.
"""

import os
import re
import sys
from pathlib import Path

# Machine-specific: set both to `metrion_binary_path` and `metrion_db_dir` before installing.
METRION_BINARY = Path("/opt/metrion-runtime/venv/bin/metrion")
DB_ROOT = Path("/home/metrilens/metrion-data")

# The fixed tail of METRION's analyze argv. `--filter-cmdline .` is a deliberately
# unfiltered export, which `attribution.py` then narrows itself; METRION requires at
# least one filter, so the flag cannot simply be dropped.
FIXED_ANALYZE_ARGS = (
    "--filter-cmdline",
    ".",
    "--type",
    "duckdb",
    "--export-summary",
    "--export-raw-data",
    "--no-plots",
)

TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

_USAGE = "usage: metrion_analyze_wrapper.py <db-path> <start-time> <end-time>"


class ValidationError(Exception):
    """A rejected argument. Message is written to stderr and the exit code is non-zero."""


def _validate_db_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_symlink():
        raise ValidationError(f"database path is a symlink, refusing to follow it: {raw_path!r}")
    resolved = path.resolve()
    root = DB_ROOT.resolve()
    if resolved.parent != root:
        raise ValidationError(
            f"database path {resolved} is not directly inside {root}; "
            "MetriLens's `metrion_db_dir` must be set to exactly that directory"
        )
    if not resolved.is_file():
        raise ValidationError(f"database path is not a regular file: {resolved}")
    return resolved


def _validate_timestamp(raw: str, *, label: str) -> str:
    if not TIMESTAMP_PATTERN.match(raw):
        raise ValidationError(f"{label} {raw!r} is not in METRION's 'YYYY-MM-DD HH:MM:SS' form")
    return raw


def build_argv(db_path: str, start_time: str, end_time: str) -> list[str]:
    """Validate the three varying values and build the full argv to exec.

    Separate from `main` so the validation can be tested without exec'ing anything.
    """
    resolved_db = _validate_db_path(db_path)
    start = _validate_timestamp(start_time, label="start time")
    end = _validate_timestamp(end_time, label="end time")
    return [
        str(METRION_BINARY),
        "analyze",
        "--db-path",
        str(resolved_db),
        "--start-time",
        start,
        "--end-time",
        end,
        *FIXED_ANALYZE_ARGS,
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"expected exactly 3 arguments, got {len(argv)}\n{_USAGE}", file=sys.stderr)
        return 2
    try:
        command = build_argv(*argv)
    except ValidationError as exc:
        print(f"refusing to run 'metrion analyze': {exc}", file=sys.stderr)
        return 2
    if not METRION_BINARY.is_file():
        print(f"METRION binary not found at {METRION_BINARY}", file=sys.stderr)
        return 2
    os.execv(str(METRION_BINARY), command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
