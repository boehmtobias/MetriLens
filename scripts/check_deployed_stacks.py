"""Pre-session check on the target app's deployed `.env` files.

`tests/test_stack_definitions.py` already asserts that the tracked `*.env.example` files differ
in exactly the intended places. This checks the thing that actually runs: the `.env` each Compose
project was deployed with, under `stacks_dir`. The two can disagree in ways nothing else notices:
a placeholder never substituted, a password that does not match the baseline, a copy that picked
up the wrong example, an edit made on the machine and never carried back.

The failure this exists to prevent is not a crash. A stack whose admin password does not match
MetriLens's own `.env` fails its readiness login and the repetition retries, which looks like
flakiness; a stack that quietly carries the wrong `INVENTREE_DEBUG` produces a clean session
measuring nothing, which is worse, because the report will not say so.

Never prints a secret: values that must match are reported only as matching or differing, so
the output is safe to share from a shared machine.

Run as whoever owns the deployed files, since they are mode 600:

    sudo -u metrilens .venv/bin/python scripts/check_deployed_stacks.py
"""

import os
import sys
from pathlib import Path

from metrilens.orchestrator import load_stacks_dir  # noqa: E402

# Compose project directory -> (INVENTREE_DEBUG, INVENTREE_CACHE_ENABLED) it must carry. The
# baseline is the reference every other stack is diffed against.
BASELINE = "inventree-baseline"
ARMS = {
    BASELINE: ("False", "True"),
    "inventree-debug-true": ("True", "True"),
    "inventree-cache-off": ("False", "False"),
    "inventree-modified": ("True", "False"),
}
# Everything a stack may legitimately differ from the baseline in, beyond the two factors: the
# bookkeeping that lets several stacks coexist on one host.
ALLOWED_DIFFERING = {
    "INVENTREE_DEBUG",
    "INVENTREE_CACHE_ENABLED",
    "COMPOSE_PROJECT_NAME",
    "INVENTREE_SITE_URL",
    "INVENTREE_HTTP_PORT",
    "INVENTREE_HTTPS_PORT",
}
# Must be byte-identical everywhere: the readiness login and the Playwright scripts authenticate
# with MetriLens's own copies, and a session must not measure an InvenTree version difference on
# top of the factors it is isolating.
MUST_MATCH = (
    "INVENTREE_ADMIN_USER",
    "INVENTREE_ADMIN_PASSWORD",
    "INVENTREE_DB_PASSWORD",
    "INVENTREE_TAG",
)
PLACEHOLDERS = ("__ADMIN_PASSWORD__", "__DB_PASSWORD__")


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def main() -> int:
    stacks_dir = load_stacks_dir(Path("config.yaml"))
    if stacks_dir is None:
        print("FAIL  config.yaml sets no stacks_dir, so there is nothing to check")
        return 1
    print(f"stacks_dir: {stacks_dir}\n")

    envs, failures = {}, []
    for name in ARMS:
        path = stacks_dir / name / ".env"
        if not path.is_file():
            print(f"FAIL  {name}: no .env at {path}")
            failures.append(name)
            continue
        try:
            envs[name] = read_env(path)
        except PermissionError:
            print(f"FAIL  {name}: .env unreadable as uid {os.geteuid()}; run as its owner")
            failures.append(name)
    if BASELINE not in envs:
        print(f"\nFAIL  {BASELINE} is the reference every other arm is compared against")
        return 1
    baseline = envs[BASELINE]

    for name, (debug, cache) in ARMS.items():
        env = envs.get(name)
        if env is None:
            continue
        problems = []
        raw = (stacks_dir / name / ".env").read_text()
        for token in PLACEHOLDERS:
            if token in raw:
                problems.append(f"{token} was never substituted")
        if env.get("INVENTREE_DEBUG") != debug:
            problems.append(
                f"INVENTREE_DEBUG is {env.get('INVENTREE_DEBUG')!r}, expected {debug!r}"
            )
        if env.get("INVENTREE_CACHE_ENABLED") != cache:
            problems.append(
                f"INVENTREE_CACHE_ENABLED is {env.get('INVENTREE_CACHE_ENABLED')!r}, "
                f"expected {cache!r}"
            )
        if name != BASELINE:
            missing = set(baseline) - set(env)
            extra = set(env) - set(baseline)
            if missing:
                problems.append(f"keys missing vs baseline: {sorted(missing)}")
            if extra:
                problems.append(f"keys not in baseline: {sorted(extra)}")
            differing = {k for k in baseline if k in env and baseline[k] != env[k]}
            unexpected = differing - ALLOWED_DIFFERING
            if unexpected:
                problems.append(f"differs from baseline in {sorted(unexpected)}, which it must not")
            for key in MUST_MATCH:
                if key in env and key in baseline and env[key] != baseline[key]:
                    problems.append(f"{key} differs from the baseline's (values not shown)")
        print(
            f"{'FAIL' if problems else 'PASS'}  {name:<24} "
            f"DEBUG={env.get('INVENTREE_DEBUG')} cache={env.get('INVENTREE_CACHE_ENABLED')} "
            f"port={env.get('INVENTREE_HTTP_PORT')}"
        )
        for problem in problems:
            print(f"        {problem}")
        if problems:
            failures.append(name)

    for key in ("COMPOSE_PROJECT_NAME", "INVENTREE_HTTP_PORT", "INVENTREE_HTTPS_PORT"):
        seen = [env[key] for env in envs.values() if key in env]
        if len(set(seen)) != len(seen):
            print(f"FAIL  {key} collides across arms: {seen}")
            failures.append(key)

    metrilens_env = Path(".env")
    if metrilens_env.is_file():
        own = read_env(metrilens_env)
        theirs = baseline.get("INVENTREE_ADMIN_PASSWORD")
        mine = own.get("INVENTREE_ADMIN_PASSWORD")
        if mine is None:
            print(
                "\nWARN  MetriLens's own .env sets no INVENTREE_ADMIN_PASSWORD; if it is "
                "exported in the environment instead, that wins and this check cannot see it"
            )
        elif mine != theirs:
            print(
                "\nFAIL  MetriLens's own .env admin password differs from the stacks' "
                "(values not shown); every readiness login would fail"
            )
            failures.append(".env")
        else:
            print("\nPASS  MetriLens's own .env admin password matches the stacks'")
    else:
        print("\nWARN  no MetriLens .env in the working directory; run this from the repo root")

    print()
    if failures:
        print(f"NOT READY: {len(set(failures))} problem(s) above")
        return 1
    print("READY: all four arms differ from the baseline only where intended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
