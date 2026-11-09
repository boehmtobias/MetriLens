"""Host facts recorded on each comparison session.

The CPU governor and hardware context are read at session start rather than configured, so
they cannot go stale the way a written description can. The environment checker and the
orchestrator share these readers.

No reader may fail a session: `collect_host_facts` reads each fact independently and records
`None` for one it cannot read (`docker --version`, for example, may be unavailable to the
account running the session).
"""

import hashlib
import os
import platform
import re
import subprocess
from pathlib import Path

CPU_ROOT = Path("/sys/devices/system/cpu")
CPUINFO_PATH = Path("/proc/cpuinfo")
MEMINFO_PATH = Path("/proc/meminfo")
OS_RELEASE_PATH = Path("/etc/os-release")
POWERCAP_ROOT = Path("/sys/class/powercap")
REQUIRED_GOVERNOR = "performance"

# What a file has to be to count towards METRION's fingerprint. Source and headers only:
# `.pyc` files are rebuilt on import and would make the hash depend on when the tree was
# last executed rather than on what it contains.
_METRION_HASHED_SUFFIXES = frozenset({".py", ".h", ".c"})
_SUBPROCESS_TIMEOUT_S = 10


def cpu_governor_paths(cpu_root: Path) -> list[Path]:
    """The per-CPU `scaling_governor` files, sorted.

    Exposed separately so the glob pattern exists once: the checker needs the count for
    its message, and the recorded value needs the contents.
    """
    return sorted(cpu_root.glob("cpu[0-9]*/cpufreq/scaling_governor"))


def read_cpu_governors(cpu_root: Path) -> list[str]:
    """Every distinct `scaling_governor` value across the CPUs, sorted.

    `cpu_root` is a parameter rather than a module lookup so both call sites pass their
    own, which is what keeps the checker's tests able to point it at a temp directory.

    Returns an empty list when there is no `cpufreq` at all, which is a real state and
    not an error: a VM or a kernel without the driver has no governor to report, and the
    caller decides whether that blocks (the checker) or is recorded as unknown (a
    session).
    """
    return sorted({path.read_text().strip() for path in cpu_governor_paths(cpu_root)})


def describe_cpu_governors(governors: list[str]) -> str | None:
    """The value stored on `comparison_session.cpu_governor`.

    A plain comma-joined string so it stays queryable (`where cpu_governor = 'performance'`).
    A mixed reading records as e.g. `'performance,powersave'`, which `check_environment.py
    --mode real` rejects before a session. `None` for a host with no `cpufreq`.
    """
    return ",".join(governors) or None


def _first_line_field(path: Path, key: str) -> str | None:
    """The value of `key: value` in a `/proc` file, first occurrence only."""
    for line in path.read_text().splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() == key:
            return value.strip() or None
    return None


def read_cpu_model() -> str | None:
    return _first_line_field(CPUINFO_PATH, "model name")


def read_cpu_count() -> int:
    """Schedulable CPUs, not `os.cpu_count()`, so a cgroup-restricted run reports what it has."""
    return len(os.sched_getaffinity(0))


def read_ram_bytes() -> int | None:
    value = _first_line_field(MEMINFO_PATH, "MemTotal")
    if value is None:
        return None
    match = re.match(r"(\d+)\s*kB", value)
    return int(match.group(1)) * 1024 if match else None


def read_kernel_release() -> str:
    return os.uname().release


def read_distro() -> str | None:
    """`NAME VERSION` from os-release, quotes stripped, which is what a write-up quotes."""
    fields: dict[str, str] = {}
    for line in OS_RELEASE_PATH.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value.strip().strip('"')
    name, version = fields.get("NAME"), fields.get("VERSION")
    return " ".join(part for part in (name, version) if part) or None


def read_virtualization() -> str | None:
    """`systemd-detect-virt`'s verdict; `"none"` means bare metal.

    The exit code is deliberately ignored: it is 1 when no virtualization is detected, so
    checking it would record "unavailable" for a bare-metal host.
    """
    result = subprocess.run(
        ["systemd-detect-virt"], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S
    )
    return result.stdout.strip() or None


def read_rapl_domains() -> list[str]:
    """The powercap domain names present, which is where the absent `dram` domain shows up."""
    return sorted(path.name for path in POWERCAP_ROOT.glob("intel-rapl*"))


def read_docker_version() -> str | None:
    result = subprocess.run(
        ["docker", "--version"], capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S
    )
    return result.stdout.strip() or None


def metrion_site_packages(metrion_binary_path: Path) -> Path | None:
    """METRION's package directory in its venv's `site-packages`, or `None` if absent.

    Globbed rather than built from a fixed Python version, so it keeps working after an
    interpreter upgrade in METRION's venv.
    """
    venv = metrion_binary_path.parent.parent
    candidates = sorted(venv.glob("lib/python*/site-packages/metrion"))
    return candidates[0] if candidates else None


def read_metrion_version(metrion_binary_path: Path) -> str | None:
    """From the installed `dist-info` directory name, the only version the install carries."""
    package = metrion_site_packages(metrion_binary_path)
    if package is None:
        return None
    dist_infos = sorted(package.parent.glob("metrion-*.dist-info"))
    return dist_infos[0].name.removesuffix(".dist-info") if dist_infos else None


def read_metrion_package_sha256(metrion_binary_path: Path) -> str | None:
    """SHA-256 over the installed METRION package's source and header files and their paths.

    A rebuild or reinstall can change METRION's behaviour without any error. The hash cannot
    name the build, but it shows whether two sessions ran the same one.
    """
    package = metrion_site_packages(metrion_binary_path)
    if package is None:
        return None
    digest = hashlib.sha256()
    for path in sorted(
        candidate
        for candidate in package.rglob("*")
        if candidate.is_file() and candidate.suffix in _METRION_HASHED_SUFFIXES
    ):
        digest.update(str(path.relative_to(package)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def collect_host_facts(metrion_binary_path: Path | None = None) -> dict:
    """The value stored on `comparison_session.host_facts`.

    Each reader is attempted independently, so one unreadable fact costs only itself. The
    keys are always present, which matters for reading a session years later: a missing key
    would be ambiguous between "this version did not collect it" and "it could not be read",
    while an explicit `None` says only the second.
    """
    readers = {
        "cpu_model": read_cpu_model,
        "cpu_count": read_cpu_count,
        "ram_bytes": read_ram_bytes,
        "kernel_release": read_kernel_release,
        "distro": read_distro,
        "virtualization": read_virtualization,
        "rapl_domains": read_rapl_domains,
        "python_version": platform.python_version,
        "docker_version": read_docker_version,
    }
    if metrion_binary_path is not None:
        readers["metrion_version"] = lambda: read_metrion_version(metrion_binary_path)
        readers["metrion_package_sha256"] = lambda: read_metrion_package_sha256(metrion_binary_path)
    else:
        readers["metrion_version"] = lambda: None
        readers["metrion_package_sha256"] = lambda: None

    facts: dict = {}
    for name, reader in readers.items():
        try:
            facts[name] = reader()
        except (OSError, ValueError, subprocess.SubprocessError):
            facts[name] = None
    return facts
