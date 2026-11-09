"""Validates a repetition's artifacts and writes its `repetition` row.

Parses METRION's export and the step log into the models from `validation.py`, then moves the
row from `RUNNING` to `OK` or `FAILED_VALIDATION`. The orchestrator archives the raw artifacts
under `<data_dir>/<comparison_session id>/slot_NNN/`; this module only records the paths it is
given on `metrion_db_ref`/`raw_export_ref`/`step_log_ref`.
"""

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from metrilens.attribution import MAX_MISSING_INTERVALS, container_energy_j, interval_coverage
from metrilens.models import Repetition
from metrilens.validation import MetrionExport, StepLog


@dataclass
class CanonicalCounts:
    """A scenario/version's canonical step/action/entity counts from the pre-flight
    dry run, supplied by the orchestrator so this module never has to know about
    `equivalence_check`'s schema or which side (a/b) a version is.
    """

    step_count: int
    action_count: int
    entity_count: int


def _drift_notes(step_log: StepLog, canonical: CanonicalCounts) -> str | None:
    """Compare an `OK` repetition's counts against the dry run's canonical ones.

    A data-quality signal about script/environment stability distinct from the
    version-equivalence question the pre-flight gate already answered; recorded
    as a note, never blocks the repetition from completing.
    """
    mismatches = []
    if step_log.step_count != canonical.step_count:
        mismatches.append(f"step_count {step_log.step_count} != canonical {canonical.step_count}")
    if step_log.action_count != canonical.action_count:
        mismatches.append(
            f"action_count {step_log.action_count} != canonical {canonical.action_count}"
        )
    if step_log.entity_count != canonical.entity_count:
        mismatches.append(
            f"entity_count {step_log.entity_count} != canonical {canonical.entity_count}"
        )
    if not mismatches:
        return None
    return "drift vs pre-flight canonical counts: " + "; ".join(mismatches)


def _container_total_j(raw_cpu_export_path: Path, container_map: dict[str, str]) -> float:
    """The repetition's CPU energy: METRION's per-thread rows summed over the target app's
    containers.

    `analyze` runs unfiltered, so the export's summary total covers the whole host. The target
    app's rows are selected here by cgroup instead (see `attribution`). Summing the raw export
    over a window reproduces `analyze`'s own total, so this is a row selection, not a second
    attribution model.
    """
    totals = container_energy_j(raw_cpu_export_path, container_map)
    if not totals:
        raise ValueError(
            f"{raw_cpu_export_path} holds no rows belonging to the target app's containers, "
            "so this repetition has no energy to report"
        )
    return sum(totals.values())


def _check_window_coverage(raw_cpu_export_path: Path, requested_seconds: float) -> None:
    """Fail a repetition whose export covers clearly less than the measured window.

    A healthy export is a second or two short, because `analyze` drops a boundary interval it
    cannot fully contain. This catches a much larger gap, such as a monitor killed during
    startup, which would otherwise produce a plausible but far too low reading. It is what lets
    the orchestrator keep a repetition whose monitor had to be killed.
    """
    covered = interval_coverage(raw_cpu_export_path)
    if covered + MAX_MISSING_INTERVALS < requested_seconds:
        raise ValueError(
            f"{raw_cpu_export_path} covers {covered} of the {requested_seconds:.0f} requested "
            f"seconds, more than {MAX_MISSING_INTERVALS} short, so this repetition's window was "
            "only partly measured. Not recoverable by re-reading: the usual cause is METRION's "
            "package-energy sampler stopping before the window end, so the missing seconds were "
            "never collected"
        )


def write_repetition(
    session: Session,
    repetition: Repetition,
    *,
    metrion_export_path: Path,
    raw_cpu_export_path: Path,
    container_map: dict[str, str],
    step_log_path: Path,
    metrion_db_ref: str,
    raw_export_ref: str,
    canonical_counts: CanonicalCounts | None = None,
) -> Repetition:
    """Validate a repetition's artifacts and write it through its state machine.

    `repetition` must already exist in `RUNNING` (the orchestrator creates it before
    running the version's script). On successful parse, populates the measurement
    fields and fires `complete`; on a parse failure, records `validation_notes` and
    fires `fail_validation` instead, so repetition counts stay honest.
    """
    try:
        export = MetrionExport.from_json_file(metrion_export_path)
        step_log = StepLog.from_json_file(step_log_path)
        _check_window_coverage(raw_cpu_export_path, export.metadata.duration_seconds)
        cpu_energy_j = _container_total_j(raw_cpu_export_path, container_map)
    except (ValidationError, OSError, ValueError) as exc:
        repetition.validation_notes = f"validation failed: {exc}"
        repetition.fail_validation()
        session.add(repetition)
        session.commit()
        return repetition

    repetition.start_time = step_log.start_time
    repetition.end_time = step_log.end_time
    repetition.cpu_energy_j = cpu_energy_j
    # DRAM and GPU stay null under an unfiltered analyze rather than carrying the export's
    # machine-wide totals, which are not this workload's. METRION doesn't decompose either per
    # container, so there is nothing to select the target stack's share from; `None` is the
    # honest signal, exactly as it already is on hardware with no DRAM RAPL domain.
    repetition.dram_energy_j = None
    repetition.gpu_energy_j = None
    repetition.runtime_s = export.metadata.duration_seconds
    # Computed here for the same reason `cpu_energy_j` is: the export's own `average_power_w`
    # describes the whole host, not this workload. Keeps METRION's convention of dividing by the
    # *requested* window rather than by the samples actually covered, which makes this a lower
    # bound on the true average power.
    repetition.average_power_w = cpu_energy_j / export.metadata.duration_seconds
    repetition.metrion_db_ref = str(metrion_db_ref)
    repetition.raw_export_ref = str(raw_export_ref)
    repetition.step_log_ref = str(step_log_path)

    if canonical_counts is not None:
        repetition.validation_notes = _drift_notes(step_log, canonical_counts)

    repetition.complete()
    session.add(repetition)
    session.commit()
    return repetition
