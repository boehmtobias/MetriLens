"""The host facts a session records, including the CPU governor it ran under."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from metrilens import host as host_module
from metrilens.host import (
    REQUIRED_GOVERNOR,
    collect_host_facts,
    cpu_governor_paths,
    describe_cpu_governors,
    metrion_site_packages,
    read_cpu_governors,
    read_cpu_model,
    read_distro,
    read_metrion_package_sha256,
    read_metrion_version,
    read_ram_bytes,
    read_virtualization,
)


def _fake_cpus(root: Path, governors: list[str]) -> None:
    for index, governor in enumerate(governors):
        path = root / f"cpu{index}" / "cpufreq"
        path.mkdir(parents=True)
        (path / "scaling_governor").write_text(governor + "\n")


class TestReadCpuGovernors:
    def test_a_uniform_machine_reports_one_value(self, tmp_path):
        _fake_cpus(tmp_path, [REQUIRED_GOVERNOR] * 24)
        assert read_cpu_governors(tmp_path) == [REQUIRED_GOVERNOR]

    def test_a_mixed_machine_reports_every_value(self, tmp_path):
        """The case the pre-session check blocks on, so it should never reach a row, but a
        column that silently reported the first CPU's value would hide it if it ever did."""
        _fake_cpus(tmp_path, ["performance", "powersave", "performance"])
        assert read_cpu_governors(tmp_path) == ["performance", "powersave"]

    def test_no_cpufreq_is_empty_rather_than_an_error(self, tmp_path):
        assert read_cpu_governors(tmp_path) == []

    def test_trailing_newlines_are_stripped(self, tmp_path):
        """sysfs writes a trailing newline; an unstripped value would never match a query."""
        _fake_cpus(tmp_path, ["performance"])
        assert read_cpu_governors(tmp_path) == ["performance"]

    def test_paths_and_values_come_from_the_same_glob(self, tmp_path):
        """The count in the checker's message and the recorded value must not disagree."""
        _fake_cpus(tmp_path, ["performance"] * 4)
        assert len(cpu_governor_paths(tmp_path)) == 4
        assert read_cpu_governors(tmp_path) == ["performance"]


class TestDescribeCpuGovernors:
    @pytest.mark.parametrize(
        ("governors", "expected"),
        [
            (["performance"], "performance"),
            (["performance", "powersave"], "performance,powersave"),
            ([], None),
        ],
    )
    def test_stored_value_stays_queryable(self, governors, expected):
        assert describe_cpu_governors(governors) == expected


def _metrion_tree(root: Path, files: dict[str, str], version: str = "0.1.0") -> Path:
    """A stand-in for METRION's install: `<venv>/bin/metrion` plus its site-packages."""
    binary = root / "bin" / "metrion"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    site = root / "lib" / "python3.12" / "site-packages"
    (site / f"metrion-{version}.dist-info").mkdir(parents=True)
    for name, content in files.items():
        path = site / "metrion" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return binary


class TestProcReaders:
    def test_cpu_model_takes_the_first_match(self, tmp_path, monkeypatch):
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text(
            "processor\t: 0\nmodel name\t: Intel(R) Core(TM) Ultra 9 285K\n"
            "processor\t: 1\nmodel name\t: Intel(R) Core(TM) Ultra 9 285K\n"
        )
        monkeypatch.setattr(host_module, "CPUINFO_PATH", cpuinfo)
        assert read_cpu_model() == "Intel(R) Core(TM) Ultra 9 285K"

    def test_ram_is_converted_from_kb_to_bytes(self, tmp_path, monkeypatch):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       63892932 kB\nMemFree:  100 kB\n")
        monkeypatch.setattr(host_module, "MEMINFO_PATH", meminfo)
        assert read_ram_bytes() == 63892932 * 1024

    def test_ram_is_none_rather_than_wrong_when_the_unit_is_unexpected(self, tmp_path, monkeypatch):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       63892932 MB\n")
        monkeypatch.setattr(host_module, "MEMINFO_PATH", meminfo)
        assert read_ram_bytes() is None

    def test_distro_strips_the_quotes_os_release_uses(self, tmp_path, monkeypatch):
        os_release = tmp_path / "os-release"
        os_release.write_text('NAME="Ubuntu"\nVERSION="26.04 LTS (Resolute Raccoon)"\nID=ubuntu\n')
        monkeypatch.setattr(host_module, "OS_RELEASE_PATH", os_release)
        assert read_distro() == "Ubuntu 26.04 LTS (Resolute Raccoon)"


class TestReadVirtualization:
    def test_bare_metal_is_recorded_despite_the_nonzero_exit(self, monkeypatch):
        """`systemd-detect-virt` exits **1** when it finds no virtualization.

        So a reader that checked the return code would record "unavailable" for exactly the
        state this project most wants on record, bare metal. This test stops that check being
        added later as a tidy-up.
        """
        monkeypatch.setattr(
            host_module.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="none\n", stderr="", returncode=1),
        )
        assert read_virtualization() == "none"

    def test_a_detected_hypervisor_is_reported_by_name(self, monkeypatch):
        monkeypatch.setattr(
            host_module.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="kvm\n", stderr="", returncode=0),
        )
        assert read_virtualization() == "kvm"


class TestMetrionFingerprint:
    def test_site_packages_is_found_by_glob_not_a_hardcoded_interpreter(self, tmp_path):
        """`python3.12` is hardcoded all over the setup doc; the fingerprint must not repeat it."""
        binary = _metrion_tree(tmp_path, {"__init__.py": "x\n"})
        found = metrion_site_packages(binary)
        assert found is not None
        assert found.parent.parent.name.startswith("python")

    def test_version_comes_from_the_dist_info_name(self, tmp_path):
        binary = _metrion_tree(tmp_path, {"__init__.py": "x\n"}, version="0.2.7")
        assert read_metrion_version(binary) == "metrion-0.2.7"

    def test_missing_install_reads_as_none_rather_than_raising(self, tmp_path):
        binary = tmp_path / "bin" / "metrion"
        binary.parent.mkdir(parents=True)
        binary.write_text("")
        assert metrion_site_packages(binary) is None
        assert read_metrion_version(binary) is None
        assert read_metrion_package_sha256(binary) is None

    def test_hash_is_stable_for_an_unchanged_tree(self, tmp_path):
        binary = _metrion_tree(tmp_path, {"a.py": "one\n", "helpers/b.h": "two\n"})
        assert read_metrion_package_sha256(binary) == read_metrion_package_sha256(binary)

    def test_hash_changes_when_a_patched_file_changes(self, tmp_path):
        """The failure this exists for: a rebuild reverting the local patches silently."""
        binary = _metrion_tree(tmp_path, {"a.py": "patched\n"})
        patched = read_metrion_package_sha256(binary)
        (binary.parent.parent / "lib/python3.12/site-packages/metrion/a.py").write_text("stock\n")
        assert read_metrion_package_sha256(binary) != patched

    def test_hash_is_path_sensitive(self, tmp_path):
        """Content alone would collide between a file and the same content moved elsewhere."""
        first = _metrion_tree(tmp_path / "one", {"a.py": "same\n"})
        second = _metrion_tree(tmp_path / "two", {"b.py": "same\n"})
        assert read_metrion_package_sha256(first) != read_metrion_package_sha256(second)

    def test_pyc_files_are_ignored(self, tmp_path):
        """Otherwise the hash depends on when the tree was last imported, not what it holds."""
        binary = _metrion_tree(tmp_path, {"a.py": "one\n"})
        before = read_metrion_package_sha256(binary)
        cache = binary.parent.parent / "lib/python3.12/site-packages/metrion/__pycache__"
        cache.mkdir()
        (cache / "a.cpython-312.pyc").write_bytes(b"\x00\x01compiled")
        assert read_metrion_package_sha256(binary) == before


class TestCollectHostFacts:
    EXPECTED_KEYS = {
        "cpu_model",
        "cpu_count",
        "ram_bytes",
        "kernel_release",
        "distro",
        "virtualization",
        "rapl_domains",
        "python_version",
        "docker_version",
        "metrion_version",
        "metrion_package_sha256",
    }

    def test_every_key_is_always_present(self):
        """A missing key would be ambiguous between "not collected" and "not readable"."""
        assert set(collect_host_facts()) == self.EXPECTED_KEYS

    def test_metrion_fields_are_none_without_a_binary_path(self):
        facts = collect_host_facts()
        assert facts["metrion_version"] is None
        assert facts["metrion_package_sha256"] is None

    def test_one_unreadable_fact_costs_only_itself(self, monkeypatch):
        """Provenance is worth less than the measurement, so no reader may fail a session."""

        def boom():
            raise OSError("permission denied")

        monkeypatch.setattr(host_module, "read_docker_version", boom)
        facts = collect_host_facts()
        assert facts["docker_version"] is None
        assert facts["kernel_release"]
        assert set(facts) == self.EXPECTED_KEYS
