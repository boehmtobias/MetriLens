"""Pre-flight sudo check for unattended `metrilens run-session` runs.

Fails if any `sudo` call the pipeline makes would prompt for a password, using
`smoke_test_session.check_passwordless_sudo`. Also runs `check_root_owned_copies`, since a
valid grant can still run a root-owned script copy that no longer matches the checkout.
"""

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import smoke_test_session  # noqa: E402

from metrilens.orchestrator import load_session_config  # noqa: E402


def main() -> int:
    config = load_session_config(Path("config.yaml"), Path("data/raw"), socket.gethostname())
    problems = smoke_test_session.check_passwordless_sudo(config)

    # Reported separately from the sudo problems because it is a different failure: the grant is
    # fine and root would run the wrong code. Both are checked here so the one pre-flight covers
    # every root-owned copy's permission and its content.
    drift = smoke_test_session.check_root_owned_copies(config)

    if problems:
        print(f"sudo would prompt for a password for: {', '.join(problems)}", file=sys.stderr)
    for problem in drift:
        print(f"root-owned copy problem: {problem}", file=sys.stderr)
    if problems or drift:
        return 1

    print("sudo check OK: all pipeline sudo calls are passwordless")
    print("root-owned copies OK:")
    for path in (config.batch_cleanup_script_path, config.analyze_wrapper_path):
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
