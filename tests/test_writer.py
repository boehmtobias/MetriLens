from pathlib import Path

from metrilens.models import Repetition, RepetitionStatus
from metrilens.writer import CanonicalCounts, write_repetition

_SERVER_CGROUP = "/system.slice/docker-abc123def456xx.scope"
_FOREIGN_CGROUP = "/system.slice/docker-000000000000xx.scope"
CONTAINER_MAP = {"abc123def456": "inventree-server", "fedcba987654": "inventree-db"}
_RAW_EXPORT_HEADER = (
    "interval_start,interval_end,cmdline,executable_name,resource_group_name,"
    "resource_group_id,total_energy_j,active_energy_j,idle_energy_j\n"
)


def _write_raw_cpu_export(
    path: Path, rows: tuple[tuple[str, float], ...] = (), intervals: int = 23
) -> Path:
    """One repetition's per-thread CPU export, which is where the repetition's own
    `cpu_energy_j` comes from, so every writer test needs one.

    `intervals` is how many distinct one-second sampling intervals the export spans, which the
    coverage check reads. Padded with zero-energy rows so the count can be set independently
    of the energy under test, the way a real export carries many threads per interval.
    """
    rows = rows or ((_SERVER_CGROUP, 8.0), ("/system.slice/docker-fedcba987654xx.scope", 4.3))
    lines = [_RAW_EXPORT_HEADER]
    for group, energy in rows:
        lines.append(f"i0,i1,cmd,exe,{group},,{energy},{energy},0\n")
    # Padding sits in a cgroup outside the map on purpose: it must add interval coverage without
    # adding target-stack rows, so a test can still exercise "no rows from the target app".
    for index in range(1, intervals):
        lines.append(f"i{index},i{index + 1},cmd,exe,{_FOREIGN_CGROUP},,0,0,0\n")
    path.write_text("".join(lines))
    return path


def _repetition(session, version_run) -> Repetition:
    rep = Repetition(version_run_id=version_run.id, repetition_index=0)
    session.add(rep)
    session.flush()
    return rep


class TestWriteRepetitionSuccess:
    def test_completes_and_populates_measurement_fields(
        self, session, version_run, tmp_path, write_json, valid_metrion_export, valid_step_log
    ):
        rep = _repetition(session, version_run)
        export_path = write_json(tmp_path / "export.json", valid_metrion_export)
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "raw_cpu.csv")

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="data/raw/1/metrion.db",
            raw_export_ref="data/raw/1/raw_cpu.csv",
        )

        assert result.status is RepetitionStatus.OK
        assert result.cpu_energy_j == 12.3  # 8.0 + 4.3
        assert result.dram_energy_j is None
        assert result.runtime_s == 22.5
        assert result.average_power_w == 12.3 / 22.5
        assert result.metrion_db_ref == "data/raw/1/metrion.db"
        assert result.raw_export_ref == "data/raw/1/raw_cpu.csv"
        assert result.step_log_ref == str(step_log_path)
        assert result.validation_notes is None

    def test_no_drift_note_when_canonical_counts_match(
        self, session, version_run, tmp_path, write_json, valid_metrion_export, valid_step_log
    ):
        rep = _repetition(session, version_run)
        export_path = write_json(tmp_path / "export.json", valid_metrion_export)
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "raw_cpu.csv")

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
            canonical_counts=CanonicalCounts(step_count=2, action_count=3, entity_count=1),
        )

        assert result.status is RepetitionStatus.OK
        assert result.validation_notes is None

    def test_a_partly_measured_window_fails_validation(
        self, session, version_run, tmp_path, write_json, valid_metrion_export, valid_step_log
    ):
        """The coverage gate, and the precondition for surviving a killed monitor. A healthy
        export is 1 to 2 s short of its request because `analyze` declines the boundary interval
        it cannot fully contain. A monitor killed during startup left 1.83 s of samples against a
        23 s request, which without this would analyze into a plausible-looking low reading."""
        rep = _repetition(session, version_run)
        export_path = write_json(tmp_path / "export.json", valid_metrion_export)
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "raw_cpu.csv", intervals=2)

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
        )

        assert result.status is RepetitionStatus.FAILED_VALIDATION
        assert "covers 2 of the 22 requested seconds" in result.validation_notes
        assert result.cpu_energy_j is None

    def test_the_usual_one_second_shortfall_is_not_a_failure(
        self, session, version_run, tmp_path, write_json, valid_metrion_export, valid_step_log
    ):
        """22 intervals for a 22.5 s request is what every healthy export in the archive looks
        like, so the gate must not reject it."""
        rep = _repetition(session, version_run)
        export_path = write_json(tmp_path / "export.json", valid_metrion_export)
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "raw_cpu.csv", intervals=22)

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
        )

        assert result.status is RepetitionStatus.OK

    def test_energy_excludes_processes_outside_the_target_stack(
        self, session, version_run, tmp_path, write_json, valid_metrion_export, valid_step_log
    ):
        """`analyze` is asked for the whole window, so the export carries every process on the
        host. The repetition's energy is the target stack's own rows, never the export's summary
        total, which describes the machine."""
        rep = _repetition(session, version_run)
        export_path = write_json(tmp_path / "export.json", valid_metrion_export)
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(
            tmp_path / "raw_cpu.csv",
            rows=(
                (_SERVER_CGROUP, 8.0),
                ("/system.slice/docker-999999999999xx.scope", 900.0),
                ("", 40.0),
            ),
        )

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
        )

        assert result.status is RepetitionStatus.OK
        assert result.cpu_energy_j == 8.0

    def test_dram_and_gpu_stay_null_rather_than_carrying_machine_wide_totals(
        self, session, version_run, tmp_path, write_json, valid_metrion_export, valid_step_log
    ):
        """METRION decomposes neither per container, so under an unfiltered analyze there is
        nothing to select this workload's share from and `None` is the honest answer."""
        rep = _repetition(session, version_run)
        valid_metrion_export["device_totals"]["DRAM"] = {"total": 5.5, "idle": 1.0, "dynamic": 4.5}
        valid_metrion_export["device_totals"]["GPU"] = {"total": 2.2, "idle": 0.2, "dynamic": 2.0}
        export_path = write_json(tmp_path / "export.json", valid_metrion_export)
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "raw_cpu.csv")

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
        )

        assert result.status is RepetitionStatus.OK
        assert result.dram_energy_j is None
        assert result.gpu_energy_j is None

    def test_an_export_without_target_stack_rows_fails_validation(
        self, session, version_run, tmp_path, write_json, valid_metrion_export, valid_step_log
    ):
        """A repetition whose export holds no row from the target app's containers has no energy
        to report, so it fails loudly rather than landing as `ok` with a total of zero."""
        rep = _repetition(session, version_run)
        export_path = write_json(tmp_path / "export.json", valid_metrion_export)
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(
            tmp_path / "raw_cpu.csv", rows=(("/system.slice/docker-999999999999xx.scope", 900.0),)
        )

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
        )

        assert result.status is RepetitionStatus.FAILED_VALIDATION
        assert "no rows belonging to the target app's containers" in result.validation_notes
        assert result.cpu_energy_j is None

    def test_drift_note_recorded_but_still_completes(
        self, session, version_run, tmp_path, write_json, valid_metrion_export, valid_step_log
    ):
        rep = _repetition(session, version_run)
        export_path = write_json(tmp_path / "export.json", valid_metrion_export)
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "raw_cpu.csv")

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
            canonical_counts=CanonicalCounts(step_count=3, action_count=3, entity_count=1),
        )

        assert result.status is RepetitionStatus.OK
        assert "step_count 2 != canonical 3" in result.validation_notes


class TestWriteRepetitionValidationFailure:
    def test_malformed_export_json_fails_validation(
        self, session, version_run, tmp_path, write_json, valid_step_log
    ):
        rep = _repetition(session, version_run)
        export_path = tmp_path / "export.json"
        export_path.write_text("not json")
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "raw_cpu.csv")

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
        )

        assert result.status is RepetitionStatus.FAILED_VALIDATION
        assert result.validation_notes is not None
        assert result.cpu_energy_j is None

    def test_step_log_count_mismatch_fails_validation(
        self, session, version_run, tmp_path, write_json, valid_metrion_export, valid_step_log
    ):
        rep = _repetition(session, version_run)
        export_path = write_json(tmp_path / "export.json", valid_metrion_export)
        valid_step_log["step_count"] = 99
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "raw_cpu.csv")

        result = write_repetition(
            session,
            rep,
            metrion_export_path=export_path,
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
        )

        assert result.status is RepetitionStatus.FAILED_VALIDATION

    def test_missing_export_file_fails_validation(
        self, session, version_run, tmp_path, write_json, valid_step_log
    ):
        rep = _repetition(session, version_run)
        step_log_path = write_json(tmp_path / "log.json", valid_step_log)
        raw_cpu_path = _write_raw_cpu_export(tmp_path / "raw_cpu.csv")

        result = write_repetition(
            session,
            rep,
            metrion_export_path=tmp_path / "does_not_exist.json",
            raw_cpu_export_path=raw_cpu_path,
            container_map=CONTAINER_MAP,
            step_log_path=step_log_path,
            metrion_db_ref="ref.db",
            raw_export_ref="ref.csv",
        )

        assert result.status is RepetitionStatus.FAILED_VALIDATION
