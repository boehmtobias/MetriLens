#!/usr/bin/env python3
"""Delete specific METRION `/tmp/batch_*.ipc` files, invoked via `sudo`.

Run as root because these files are always `root:root` (METRION runs `monitor` under
`sudo`) and `/tmp`'s sticky bit means only the file's owner or root can delete it.

Deliberately does not trust its caller's arguments at face value: `metrion_integration.
_cleanup_new_batch_files` already computes the exact before/after diff, but the
sudoers grant invoking this script still has to wildcard its trailing arguments (the
filenames are random UUIDs, unenumerable in advance), so this script re-validates
every path itself before deleting anything. That validation, not the sudoers pattern,
is what keeps this from becoming an arbitrary-file-delete primitive: each argument
must resolve to a real file directly inside `/tmp`, named `batch_*.ipc`, and must not
be a symlink (a symlink under that name could otherwise point anywhere).

No dependencies beyond the standard library, so it can run under the system `python3`
rather than needing METRION's or MetriLens's own venv.
"""

import sys
from pathlib import Path

_TMP_DIR = Path("/tmp")
_ALLOWED_PREFIX = "batch_"
_ALLOWED_SUFFIX = ".ipc"


def _validate(raw_path: str) -> Path | None:
    path = Path(raw_path)
    if path.is_symlink():
        print(f"skipping {path}: is a symlink", file=sys.stderr)
        return None
    if path.parent != _TMP_DIR:
        print(f"skipping {path}: not directly inside {_TMP_DIR}", file=sys.stderr)
        return None
    if not (path.name.startswith(_ALLOWED_PREFIX) and path.name.endswith(_ALLOWED_SUFFIX)):
        pattern = f"{_ALLOWED_PREFIX}*{_ALLOWED_SUFFIX}"
        print(f"skipping {path}: name doesn't match {pattern}", file=sys.stderr)
        return None
    if not path.is_file():
        print(f"skipping {path}: not a regular file (already gone?)", file=sys.stderr)
        return None
    return path


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: cleanup_orphan_batch_files.py <path> [<path> ...]", file=sys.stderr)
        return 1

    exit_code = 0
    for raw_path in argv:
        path = _validate(raw_path)
        if path is None:
            exit_code = 1
            continue
        try:
            path.unlink()
            print(f"deleted {path}")
        except OSError as exc:
            print(f"failed to delete {path}: {exc}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
