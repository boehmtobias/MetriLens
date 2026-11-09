import subprocess

import check_environment


def _write_rapl_domain(root, domain, name):
    domain_dir = root / domain
    domain_dir.mkdir(parents=True)
    (domain_dir / "name").write_text(name)


class TestCheckSystemd:
    def test_present(self, tmp_path, monkeypatch):
        marker = tmp_path / "system"
        marker.mkdir()
        monkeypatch.setattr(check_environment, "SYSTEMD_MARKER", marker)
        ok, _ = check_environment.check_systemd()
        assert ok is True

    def test_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(check_environment, "SYSTEMD_MARKER", tmp_path / "does-not-exist")
        ok, _ = check_environment.check_systemd()
        assert ok is False


class TestCheckRootOrSudo:
    def test_running_as_root(self, monkeypatch):
        monkeypatch.setattr(check_environment.os, "geteuid", lambda: 0)
        ok, detail = check_environment.check_root_or_sudo()
        assert ok is True
        assert "root" in detail

    def test_in_sudo_group(self, monkeypatch):
        monkeypatch.setattr(check_environment.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(check_environment.shutil, "which", lambda _: "/usr/bin/sudo")
        monkeypatch.setattr(
            check_environment.grp,
            "getgrnam",
            lambda _: type("G", (), {"gr_mem": ["alice"]})(),
        )
        monkeypatch.setattr(check_environment.os, "environ", {"USER": "alice"})
        ok, _ = check_environment.check_root_or_sudo()
        assert ok is True

    def test_not_root_and_not_in_sudo_group(self, monkeypatch):
        monkeypatch.setattr(check_environment.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(check_environment.shutil, "which", lambda _: "/usr/bin/sudo")
        monkeypatch.setattr(
            check_environment.grp, "getgrnam", lambda _: type("G", (), {"gr_mem": []})()
        )
        monkeypatch.setattr(check_environment.os, "environ", {"USER": "someone"})
        ok, _ = check_environment.check_root_or_sudo()
        assert ok is False


class TestCheckRapl:
    def test_non_intel_cpu_fails(self, tmp_path, monkeypatch):
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text("vendor_id\t: AuthenticAMD\n")
        monkeypatch.setattr(check_environment, "CPUINFO_PATH", cpuinfo)
        ok, detail = check_environment.check_rapl()
        assert ok is False
        assert "AuthenticAMD" in detail

    def test_intel_with_rapl_domains_passes(self, tmp_path, monkeypatch):
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text("vendor_id\t: GenuineIntel\n")
        monkeypatch.setattr(check_environment, "CPUINFO_PATH", cpuinfo)

        powercap = tmp_path / "powercap"
        _write_rapl_domain(powercap, "intel-rapl:0", "package-0")
        monkeypatch.setattr(check_environment, "POWERCAP_ROOT", powercap)

        ok, detail = check_environment.check_rapl()
        assert ok is True
        assert "package-0" in detail

    def test_intel_without_rapl_domains_fails(self, tmp_path, monkeypatch):
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text("vendor_id\t: GenuineIntel\n")
        monkeypatch.setattr(check_environment, "CPUINFO_PATH", cpuinfo)
        monkeypatch.setattr(check_environment, "POWERCAP_ROOT", tmp_path / "no-powercap")

        ok, _ = check_environment.check_rapl()
        assert ok is False


class TestCheckDramRapl:
    def test_dram_domain_present_passes(self, tmp_path, monkeypatch):
        powercap = tmp_path / "powercap"
        _write_rapl_domain(powercap, "intel-rapl:0:1", "dram")
        monkeypatch.setattr(check_environment, "POWERCAP_ROOT", powercap)

        ok, _ = check_environment.check_dram_rapl()
        assert ok is True

    def test_no_dram_domain_reports_unavailable(self, tmp_path, monkeypatch):
        powercap = tmp_path / "powercap"
        _write_rapl_domain(powercap, "intel-rapl:0", "package-0")
        _write_rapl_domain(powercap, "intel-rapl:0:0", "core")
        _write_rapl_domain(powercap, "intel-rapl:0:1", "uncore")
        monkeypatch.setattr(check_environment, "POWERCAP_ROOT", powercap)

        ok, _ = check_environment.check_dram_rapl()
        assert ok is False


class TestCheckCpuGovernor:
    def _write_governors(self, root, governors):
        for index, governor in enumerate(governors):
            cpufreq = root / f"cpu{index}" / "cpufreq"
            cpufreq.mkdir(parents=True)
            (cpufreq / "scaling_governor").write_text(f"{governor}\n")

    def test_all_cpus_on_performance_passes(self, tmp_path, monkeypatch):
        self._write_governors(tmp_path, ["performance"] * 4)
        monkeypatch.setattr(check_environment, "CPU_ROOT", tmp_path)

        ok, detail = check_environment.check_cpu_governor()
        assert ok is True
        assert "4 CPUs" in detail

    def test_all_cpus_on_powersave_fails(self, tmp_path, monkeypatch):
        self._write_governors(tmp_path, ["powersave"] * 4)
        monkeypatch.setattr(check_environment, "CPU_ROOT", tmp_path)

        ok, detail = check_environment.check_cpu_governor()
        assert ok is False
        assert "powersave" in detail

    def test_mixed_governors_fail_even_when_one_is_performance(self, tmp_path, monkeypatch):
        # The real failure mode on this hardware: a per-CPU write that missed some CPUs.
        self._write_governors(tmp_path, ["performance", "performance", "powersave"])
        monkeypatch.setattr(check_environment, "CPU_ROOT", tmp_path)

        ok, detail = check_environment.check_cpu_governor()
        assert ok is False
        assert "performance" in detail
        assert "powersave" in detail

    def test_no_cpufreq_at_all_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(check_environment, "CPU_ROOT", tmp_path)

        ok, detail = check_environment.check_cpu_governor()
        assert ok is False
        assert "scaling_governor" in detail

    def test_non_cpu_directories_are_ignored(self, tmp_path, monkeypatch):
        # /sys/devices/system/cpu holds cpuidle, cpufreq, microcode and friends beside cpuN.
        self._write_governors(tmp_path, ["performance"])
        for sibling in ("cpuidle", "cpufreq", "microcode"):
            (tmp_path / sibling).mkdir()
        monkeypatch.setattr(check_environment, "CPU_ROOT", tmp_path)

        ok, detail = check_environment.check_cpu_governor()
        assert ok is True
        assert "1 CPUs" in detail


class TestCheckUv:
    def test_not_on_path(self, monkeypatch):
        monkeypatch.setattr(check_environment.shutil, "which", lambda _: None)
        ok, _ = check_environment.check_uv()
        assert ok is False

    def test_version_check_succeeds(self, monkeypatch):
        monkeypatch.setattr(check_environment.shutil, "which", lambda _: "/usr/bin/uv")
        monkeypatch.setattr(
            check_environment.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="uv 0.11.31\n", stderr=""),
        )
        ok, detail = check_environment.check_uv()
        assert ok is True
        assert "0.11.31" in detail

    def test_nonzero_exit_fails(self, monkeypatch):
        monkeypatch.setattr(check_environment.shutil, "which", lambda _: "/usr/bin/uv")
        monkeypatch.setattr(
            check_environment.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="boom"),
        )
        ok, _ = check_environment.check_uv()
        assert ok is False


class TestCheckDocker:
    def test_daemon_reachable(self, monkeypatch):
        monkeypatch.setattr(check_environment.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            check_environment.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
        )
        ok, _ = check_environment.check_docker()
        assert ok is True

    def test_daemon_unreachable(self, monkeypatch):
        monkeypatch.setattr(check_environment.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.setattr(
            check_environment.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="not running"),
        )
        ok, _ = check_environment.check_docker()
        assert ok is False


class TestMainCli:
    """Covers the three-way blocking classification, not the individual checks."""

    _ALL_CHECK_NAMES = (
        "check_systemd",
        "check_root_or_sudo",
        "check_rapl",
        "check_dram_rapl",
        "check_cpu_governor",
        "check_uv",
        "check_docker",
    )

    def _patch_checks(self, monkeypatch, failing=()):
        """Make every check pass except those named in `failing`, keeping the real CHECKS table."""
        for name in self._ALL_CHECK_NAMES:
            result = (False, f"stubbed failure: {name}") if name in failing else (True, "ok")
            monkeypatch.setattr(check_environment, name, lambda result=result: result)
        monkeypatch.setattr(
            check_environment,
            "CHECKS",
            [
                (label, getattr(check_environment, fn.__name__), blocking)
                for label, fn, blocking in check_environment.CHECKS
            ],
        )

    def test_all_checks_passing_exits_zero(self, monkeypatch, runner):
        self._patch_checks(monkeypatch)
        for mode in ("dev", "real"):
            result = runner.invoke(check_environment.main, ["--mode", mode])
            assert result.exit_code == 0, mode

    def test_missing_dram_blocks_neither_mode(self, monkeypatch, runner):
        # No DRAM domain is a hardware fact to record, not a prerequisite: client-class CPUs
        # never expose it.
        self._patch_checks(monkeypatch, failing=("check_dram_rapl",))
        for mode in ("dev", "real"):
            result = runner.invoke(check_environment.main, ["--mode", mode])
            assert result.exit_code == 0, mode

    def test_wrong_governor_blocks_real_but_not_dev(self, monkeypatch, runner):
        # Real mode requires a fixed CPU governor.
        self._patch_checks(monkeypatch, failing=("check_cpu_governor",))
        assert runner.invoke(check_environment.main, ["--mode", "dev"]).exit_code == 0
        assert runner.invoke(check_environment.main, ["--mode", "real"]).exit_code == 1

    def test_always_blocking_check_fails_both_modes(self, monkeypatch, runner):
        self._patch_checks(monkeypatch, failing=("check_systemd",))
        for mode in ("dev", "real"):
            result = runner.invoke(check_environment.main, ["--mode", mode])
            assert result.exit_code == 1, mode

    def test_dram_and_governor_are_the_only_non_always_blocking_checks(self):
        # Guards the classification itself: adding a check without deciding when it blocks
        # would otherwise silently default into whatever the table says.
        by_kind = {}
        for label, _fn, blocking in check_environment.CHECKS:
            by_kind.setdefault(blocking, []).append(label)
        assert by_kind[check_environment.BLOCKING_NEVER] == ["DRAM RAPL domain"]
        assert by_kind[check_environment.BLOCKING_REAL] == ["CPU governor fixed"]
        assert len(by_kind[check_environment.BLOCKING_ALWAYS]) == 5
