"""Target-app credential resolution from the environment.

The scenario scripts and the readiness check both need the target app's login
credentials. They come from the process environment, optionally populated from a
local `.env` that is never committed, and this module is the only reader of them.

Nothing here logs or returns a resolved value alongside its variable name; errors
name the *variable*, never the value.
"""

import os
from pathlib import Path

_ENV_FILE_NAME = ".env"
_ENV_FILE_OVERRIDE_VAR = "METRILENS_ENV_FILE"


class CredentialError(RuntimeError):
    """Raised when a required credential variable is not set anywhere."""


def find_env_file(start: Path | None = None) -> Path | None:
    """Locate the `.env` to read, or `None` if there is none.

    `METRILENS_ENV_FILE` wins if set, so a session can point at a file outside
    the tree. Otherwise search upward from `start` (the working directory by
    default), then upward from this module, since a version script is invoked as
    a subprocess and its working directory is the caller's, not the repository's.
    """
    override = os.environ.get(_ENV_FILE_OVERRIDE_VAR)
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None

    roots = [start or Path.cwd(), Path(__file__).resolve().parent]
    for root in roots:
        for directory in [root.resolve(), *root.resolve().parents]:
            candidate = directory / _ENV_FILE_NAME
            if candidate.is_file():
                return candidate
    return None


def parse_env_file(text: str) -> dict[str, str]:
    """Parse `.env` text into a mapping, ignoring blank lines and `#` comments.

    Deliberately minimal, and deliberately *not* `python-dotenv`: the whole
    behaviour is a dozen lines and one of its rules (see `load_env_file`) is
    load-bearing enough to own rather than inherit from a library default.

    A trailing `#` does not start a comment, since a password may legitimately
    contain one; only a line whose first non-whitespace character is `#` is a
    comment. A `KEY=value` pair may be quoted with matching single or double
    quotes, which are stripped, so a value with leading or trailing spaces is
    still expressible.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        name, _, raw_value = stripped.partition("=")
        name = name.strip()
        if not name:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def load_env_file(path: Path | None = None) -> list[str]:
    """Populate `os.environ` from a `.env`, returning the variable names applied.

    A variable already present in the environment is never overridden. That
    is the point of doing this by hand: the real environment is the more specific
    source (a one-off `VAR=... metrilens run-session ...`, or a CI secret), and a
    stale checked-out `.env` silently winning over it is exactly the failure mode
    this rule prevents. A missing file is not an error, since the variables may
    already be set.
    """
    env_file = path or find_env_file()
    if env_file is None or not env_file.is_file():
        return []
    applied = []
    for name, value in parse_env_file(env_file.read_text()).items():
        if name not in os.environ:
            os.environ[name] = value
            applied.append(name)
    return applied


def require_env(name: str) -> str:
    """Return `name`'s value from the environment, reading `.env` if it is absent.

    Raises `CredentialError` naming the variable when it is unset or empty. Fails
    here rather than returning an empty string on purpose: an empty password
    reaches the app as a rejected login, which surfaces much later as a login
    timeout and looks like the app being slow rather than like a missing setting.
    """
    value = os.environ.get(name)
    if not value:
        load_env_file()
        value = os.environ.get(name)
    if not value:
        env_file = find_env_file()
        location = f"'{env_file}'" if env_file else f"a '{_ENV_FILE_NAME}' file"
        raise CredentialError(
            f"required environment variable '{name}' is not set; "
            f"set it in the environment or in {location} (see '.env.example')"
        )
    return value
