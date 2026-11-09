"""Environment-prerequisite checker.

Confirms systemd, root/sudo access, Intel CPU with RAPL, a fixed CPU governor, `uv`,
and Docker before a measurement session starts, rather than relying on memory of
what was checked on which machine. DRAM energy is probed and reported but never
enforced: a client-class Intel CPU has no `dram` RAPL domain at all, so its absence
is a hardware fact to record in a session's provenance rather than a prerequisite
anything can satisfy.
"""

import grp
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
from loguru import logger

from metrilens.host import (
    CPU_ROOT,
    REQUIRED_GOVERNOR,
    cpu_governor_paths,
    read_cpu_governors,
)

POWERCAP_ROOT = Path("/sys/class/powercap")
SYSTEMD_MARKER = Path("/run/systemd/system")
CPUINFO_PATH = Path("/proc/cpuinfo")
# CPU_ROOT and REQUIRED_GOVERNOR are imported from `metrilens.host`, not defined here, so
# this check and the value a session records come from one implementation. They stay
# module attributes of this script, which is what keeps them monkeypatchable.

# How a failing check is treated. Three ways, because a check can be worth reporting
# without being worth blocking on (see the module docstring).
BLOCKING_ALWAYS = "always"  # a failure stops any run, dev or real
BLOCKING_REAL = "real"  # a failure stops a real measurement session only
BLOCKING_NEVER = "never"  # recorded for provenance, never blocks


def check_systemd() -> tuple[bool, str]:
    if SYSTEMD_MARKER.exists():
        return True, "systemd is running (/run/systemd/system present)"
    return False, "/run/systemd/system not found; required for METRION's idle-power calibration"


def check_root_or_sudo() -> tuple[bool, str]:
    if os.geteuid() == 0:
        return True, "running as root"
    if shutil.which("sudo") is None:
        return False, "sudo not installed and not running as root"
    try:
        sudo_group = grp.getgrnam("sudo").gr_mem
    except KeyError:
        sudo_group = []
    username = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if username in sudo_group:
        return True, f"user '{username}' is a member of the 'sudo' group"
    return False, f"user '{username}' is not root and not in the 'sudo' group"


def _rapl_domain_names() -> list[str]:
    if not POWERCAP_ROOT.exists():
        return []
    names = []
    for entry in POWERCAP_ROOT.glob("intel-rapl:*"):
        name_file = entry / "name"
        if name_file.exists():
            names.append(name_file.read_text().strip())
    return names


def check_rapl() -> tuple[bool, str]:
    vendor = ""
    if CPUINFO_PATH.exists():
        for line in CPUINFO_PATH.read_text().splitlines():
            if line.startswith("vendor_id"):
                vendor = line.split(":", 1)[1].strip()
                break
    if vendor != "GenuineIntel":
        return False, (
            f"CPU vendor is '{vendor or 'unknown'}', not GenuineIntel; RAPL/perf assumes Intel"
        )
    names = _rapl_domain_names()
    if not names:
        return False, "no RAPL domains found under /sys/class/powercap/intel-rapl*"
    return True, f"Intel CPU with RAPL domains: {', '.join(names)}"


def check_dram_rapl() -> tuple[bool, str]:
    """Probe for DRAM attribution. Informational only; see the module docstring."""
    names = [n.lower() for n in _rapl_domain_names()]
    if any("dram" in n for n in names):
        return True, "a 'dram' RAPL domain is present, so DRAM energy is attributable"
    return False, (
        "no 'dram' RAPL domain present, so DRAM energy is unavailable on this machine "
        "(client-class CPUs don't expose one; recorded, not required)"
    )


def check_cpu_governor() -> tuple[bool, str]:
    """Confirm every CPU is on one fixed governor, which a measurement session requires."""
    paths = cpu_governor_paths(CPU_ROOT)
    governors = read_cpu_governors(CPU_ROOT)
    if not governors:
        return False, (
            f"no cpufreq 'scaling_governor' files under {CPU_ROOT}; cannot confirm a fixed governor"
        )
    if governors == [REQUIRED_GOVERNOR]:
        return True, f"all {len(paths)} CPUs on the '{REQUIRED_GOVERNOR}' governor"
    return False, (
        f"{len(paths)} CPUs report governor(s) {', '.join(governors)}, not "
        f"'{REQUIRED_GOVERNOR}' throughout; a measurement session requires a fixed governor "
        f"(`sudo cpupower frequency-set -g {REQUIRED_GOVERNOR}`)"
    )


def check_uv() -> tuple[bool, str]:
    path = shutil.which("uv")
    if path is None:
        return False, "'uv' not found on PATH"
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5)
    except OSError as exc:
        return False, f"'uv --version' failed to run: {exc}"
    if result.returncode != 0:
        return False, f"'uv --version' exited {result.returncode}: {result.stderr.strip()}"
    return True, result.stdout.strip()


def check_docker() -> tuple[bool, str]:
    path = shutil.which("docker")
    if path is None:
        return False, "'docker' not found on PATH"
    try:
        result = subprocess.run([path, "info"], capture_output=True, text=True, timeout=10)
    except OSError as exc:
        return False, f"'docker info' failed to run: {exc}"
    if result.returncode != 0:
        return False, f"'docker info' exited {result.returncode}: {result.stderr.strip()}"
    return True, "docker daemon reachable"


# (label, check function, when a failure blocks)
CHECKS = [
    ("systemd", check_systemd, BLOCKING_ALWAYS),
    ("root/sudo access", check_root_or_sudo, BLOCKING_ALWAYS),
    ("Intel CPU with RAPL", check_rapl, BLOCKING_ALWAYS),
    ("DRAM RAPL domain", check_dram_rapl, BLOCKING_NEVER),
    ("CPU governor fixed", check_cpu_governor, BLOCKING_REAL),
    ("uv available", check_uv, BLOCKING_ALWAYS),
    ("Docker daemon reachable", check_docker, BLOCKING_ALWAYS),
]


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["dev", "real"]),
    default="dev",
    show_default=True,
    help="'dev' checks only what any run needs; 'real' additionally requires what a "
    "measurement session needs, currently a fixed CPU governor. "
    "DRAM energy is reported in both modes and blocks neither.",
)
def main(mode: str) -> None:
    """Confirm this machine meets MetriLens's environment prerequisites."""
    hard_failures = []
    for label, check_fn, blocking_kind in CHECKS:
        ok, detail = check_fn()
        blocking = blocking_kind == BLOCKING_ALWAYS or (
            blocking_kind == BLOCKING_REAL and mode == "real"
        )
        if ok:
            logger.info("PASS  {}: {}", label, detail)
        elif blocking:
            logger.error("FAIL  {}: {}", label, detail)
            hard_failures.append(label)
        elif blocking_kind == BLOCKING_NEVER:
            logger.warning("NOTE  {}: {} (recorded, never blocking)", label, detail)
        else:
            logger.warning("WARN  {}: {} (allowed under --mode dev)", label, detail)

    if hard_failures:
        logger.error("Environment check failed ({} mode): {}", mode, ", ".join(hard_failures))
        sys.exit(1)

    logger.success("Environment check passed ({} mode).", mode)


if __name__ == "__main__":
    main()
