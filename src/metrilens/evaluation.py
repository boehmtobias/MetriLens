"""Evaluation and reporting for a finished `comparison_session`.

Turns the session's `repetition` rows into `summary.json` and `comparison_report.md`, which is
rendered from that summary alone, plus the figures. The per-container, per-step, per-iteration,
thread and active/idle figures re-read each repetition's archived raw per-thread export and its
`container_map.json`; a repetition without them is left out of those figures only.

Output lands under `evaluation/<host_descriptor>/<scenario_id>_<comparison_session_id>/`, never
in the SQLite database.
"""

import json
import math
import re
import textwrap
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from metrilens.attribution import (
    EnergySplit,
    container_energy_j,
    container_energy_split,
    overlap_weighted_energy_j,
    read_export_rows,
    read_role_intervals,
    rows_with_roles,
    step_energy_j,
)
from metrilens.models import ComparisonSession, EquivalenceCheck, Repetition, RepetitionStatus

# TrueType rather than matplotlib's default Type 3, so text in the PDF figures stays
# selectable/searchable and survives the font restrictions some publishers and arXiv apply to
# Type 3.
plt.rcParams["pdf.fonttype"] = 42

FIGURE_DPI = 150
TICK_LABEL_WRAP_WIDTH = 20
# Below this share of its bar's total, a stacked segment is too short to hold a legible value
# label, so it gets none rather than overprinting its neighbours.
CONTAINER_LABEL_MIN_SHARE = 0.05
# Thread names come from a key that cannot be fully trusted (`_thread_name`), so the thread
# breakdown keeps only groups holding a real share of the container's energy and sums the rest.
# The spurious variants a corrupted `comm` produces are always far below this threshold.
THREAD_MIN_SHARE = 0.02
THREAD_BREAKDOWN_MAX_GROUPS = 8
THREAD_OTHER_LABEL = "other threads"
# How many containers the thread breakdown covers, highest-energy first. Four fills a 2x2 grid;
# beyond that the panels get too small to read the per-thread values.
THREAD_BREAKDOWN_CONTAINERS = 4
# Rows of the execution-order block, chosen so a 30-per-version session fits six short lines.
EXECUTION_ORDER_ROW_WIDTH = 10

# Below this p-value the report prints `< 0.001` rather than a long run of zeros.
P_VALUE_FLOOR = 0.001


class EvaluationError(Exception):
    """Raised when a `comparison_session` can't be evaluated as specified."""


@dataclass
class _PreparedVersion:
    app_version_id: int
    label: str
    ok_repetitions: list[Repetition]
    cpu_values: list[float]
    dram_values: list[float]
    runtime_values: list[float]
    run_started_at: datetime | None = None
    run_ended_at: datetime | None = None
    # Label words shared with the other version, shown once per figure instead of on every tick.
    label_context: str = ""

    @property
    def n_retried(self) -> int:
        """Usable repetitions that needed at least one retry to produce their measurement."""
        return sum(1 for rep in self.ok_repetitions if rep.attempt_count)

    @property
    def n_noted(self) -> int:
        """Usable repetitions carrying a `validation_notes` caveat, such as a killed monitor whose
        export still covered the requested window."""
        return sum(1 for rep in self.ok_repetitions if rep.validation_notes)


def _field_values(repetitions: list[Repetition], field: str) -> list[float]:
    return [value for rep in repetitions if (value := getattr(rep, field)) is not None]


def _prepare_version(app_version, version_run) -> _PreparedVersion:
    ok_repetitions = [rep for rep in version_run.repetitions if rep.status is RepetitionStatus.OK]
    return _PreparedVersion(
        app_version_id=app_version.id,
        label=app_version.label,
        ok_repetitions=ok_repetitions,
        cpu_values=_field_values(ok_repetitions, "cpu_energy_j"),
        dram_values=_field_values(ok_repetitions, "dram_energy_j"),
        runtime_values=_field_values(ok_repetitions, "runtime_s"),
        run_started_at=version_run.started_at,
        run_ended_at=version_run.ended_at,
    )


def _thread_name(row: dict) -> str:
    """The row's kernel `comm` value, stripped of the bytes METRION sometimes leaves in it.

    `comm` is capped at 15 characters by the kernel (`TASK_COMM_LEN`), which is why real names
    arrive already truncated, as in `ThreadPoolExecu`. A small fraction of rows also carry
    uninitialized bytes past the real name. Stripping the non-printable ones still leaves
    variants separated by a stray printable byte, so callers rank by energy and bucket the tail
    (`_significant_with_other`) rather than trusting this to be a clean grouping key.
    """
    raw = row.get("executable_name") or ""
    return "".join(ch for ch in raw if 32 <= ord(ch) <= 126).strip() or "unknown"


def _parse_thread_energy(
    raw_export_path: Path, container_map: dict[str, str], role: str
) -> dict[str, float]:
    """Sum `total_energy_j` per thread name *within one container role*.

    This is the finest granularity METRION supplies and the one no container-level energy tool
    can reach at all: it decomposes a single container's total across its own threads.
    """
    totals: dict[str, float] = {}
    for row, row_role in rows_with_roles(read_export_rows(raw_export_path), container_map):
        if row_role != role:
            continue
        name = _thread_name(row)
        totals[name] = totals.get(name, 0.0) + float(row["total_energy_j"])
    return totals


def _repetition_cpu_export(rep: Repetition) -> Path | None:
    """The archived CPU per-thread export for one repetition, `None` if absent."""
    if not rep.raw_export_ref:
        return None
    paths = [Path(ref) for ref in rep.raw_export_ref.split(",")]
    cpu_path = next((path for path in paths if path.name.startswith("raw_cpu")), None)
    return cpu_path if cpu_path is not None and cpu_path.exists() else None


def _repetition_export(rep: Repetition) -> tuple[Path, dict[str, str]] | None:
    """The archived CPU export plus its sibling `container_map.json`. `None` when either
    is missing, e.g. a repetition archived before that map was captured."""
    cpu_path = _repetition_cpu_export(rep)
    if cpu_path is None:
        return None
    container_map_path = cpu_path.parent / "container_map.json"
    if not container_map_path.exists():
        return None
    return cpu_path, json.loads(container_map_path.read_text())


def _repetition_container_energy(rep: Repetition) -> dict[str, float] | None:
    export = _repetition_export(rep)
    return container_energy_j(*export) if export is not None else None


def _repetition_energy_split(rep: Repetition) -> EnergySplit | None:
    """The active/idle split over the target stack's containers only.

    Needs `container_map.json`, because the export is not pre-filtered to the target stack:
    summing every row would report the whole host's split instead. A repetition archived before
    that map was captured has no split available.
    """
    export = _repetition_export(rep)
    return container_energy_split(*export) if export is not None else None


def _naive_utc(value: datetime) -> datetime:
    """Drop tzinfo after normalizing to UTC, since METRION's export timestamps are naive UTC."""
    return value if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)


_Interval = tuple[datetime, datetime, float]
_StepSpan = tuple[str, int | None, datetime, datetime]


def _repetition_spans(rep: Repetition) -> tuple[list[_Interval], list[_StepSpan]] | None:
    """One repetition's target-stack intervals and its declared step spans, or `None`.

    `None` rather than a partial answer whenever the inputs cannot support the question: no
    archived export or container map, no step log, or a step span that does not overlap what the
    export covers. That last case is what a step log written in the wrong timezone looks like, and
    silently attributing zero to every step there would produce a decomposition that sums to
    nothing while looking like a measurement.
    """
    export = _repetition_export(rep)
    if export is None or not rep.step_log_ref:
        return None
    step_log_path = Path(rep.step_log_ref)
    if not step_log_path.exists():
        return None
    steps = json.loads(step_log_path.read_text()).get("steps") or []
    intervals = read_role_intervals(*export)
    if not steps or not intervals:
        return None

    spans = [
        (
            step["step_id"],
            step.get("iteration"),
            _naive_utc(datetime.fromisoformat(step["start_time"])),
            _naive_utc(datetime.fromisoformat(step["end_time"])),
        )
        for step in steps
    ]
    coverage_start = min(interval[0] for interval in intervals)
    coverage_end = max(interval[1] for interval in intervals)
    if spans[-1][3] <= coverage_start or spans[0][2] >= coverage_end:
        return None
    return intervals, spans


def _repetition_iteration_energy(rep: Repetition) -> dict[int, float] | None:
    """Energy per iteration for one repetition, `None` when the scenario has no repeated block.

    Steps outside the block, `run_build_order` above all, carry no iteration and are excluded
    rather than folded into the last one: it runs once per repetition by design, so charging it
    to iteration k would put a step the other iterations never paid into the comparison the
    per-iteration figure is built on.
    """
    loaded = _repetition_spans(rep)
    if loaded is None:
        return None
    intervals, spans = loaded
    totals: dict[int, float] = {}
    for _, iteration, start, end in spans:
        if iteration is None:
            continue
        totals[iteration] = totals.get(iteration, 0.0) + overlap_weighted_energy_j(
            intervals, start, end
        )
    return totals or None


def _version_iteration_series(prepared: "_PreparedVersion") -> dict[int, list[float]]:
    """Per-iteration energy as one value per repetition, over repetitions that have all of them.

    A repetition missing an iteration is dropped rather than zero-filled, unlike the per-step and
    per-container series. Those zero-fill because an absent container really did spend nothing;
    an absent iteration means the step log is incomplete, and a zero there would drag that
    iteration's mean toward zero and manufacture exactly the downward trend this figure exists to
    test for.
    """
    per_repetition = [
        breakdown
        for rep in prepared.ok_repetitions
        if (breakdown := _repetition_iteration_energy(rep)) is not None
    ]
    if not per_repetition:
        return {}
    complete = max((set(b) for b in per_repetition), key=len, default=set())
    usable = [b for b in per_repetition if set(b) >= complete]
    return {iteration: [b[iteration] for b in usable] for iteration in sorted(complete)}


def _repetition_step_energy(rep: Repetition) -> dict[str, float] | None:
    """Per-step energy for one repetition, summed across iterations.

    `None` rather than a partial answer whenever the inputs cannot support the question: no
    archived export or container map, no step log, or a step span that does not overlap what the
    export covers. That last case is what a step log written in the wrong timezone looks like, and
    silently attributing zero to every step there would produce a decomposition that sums to
    nothing while looking like a measurement.
    """
    loaded = _repetition_spans(rep)
    if loaded is None:
        return None
    intervals, spans = loaded
    return step_energy_j(intervals, [(step_id, start, end) for step_id, _, start, end in spans])


def _version_step_series(prepared: "_PreparedVersion") -> dict[str, list[float]]:
    """Per-step energy as one value per repetition, zero-filled for steps a given repetition
    lacks, so every step's series has the same length."""
    per_repetition = [
        breakdown
        for rep in prepared.ok_repetitions
        if (breakdown := _repetition_step_energy(rep)) is not None
    ]
    if not per_repetition:
        return {}
    step_ids = {step_id for breakdown in per_repetition for step_id in breakdown}
    return {
        step_id: [breakdown.get(step_id, 0.0) for breakdown in per_repetition]
        for step_id in step_ids
    }


def _version_step_breakdown(prepared: "_PreparedVersion") -> dict[str, float] | None:
    """Mean per-repetition energy per step, over whichever OK repetitions can supply one."""
    series = _version_step_series(prepared)
    return {step_id: float(np.mean(values)) for step_id, values in series.items()} or None


def _ordered_steps(declared: list[str], *breakdowns: dict[str, float] | None) -> list[str]:
    """Step ids in scenario order, with any undeclared ones appended.

    **Scenario order, never sorted**, which is the one place the per-step figures depart from
    their per-container blueprints. Containers are an unordered set, so the container figures sort
    them by name or magnitude; steps are a sequence a reader follows, and reordering
    them would break the correspondence to the step log they came from. An undeclared id should
    not occur, since `ScenarioLogger` validates against the declared list, so it is appended
    rather than dropped: a step that reached the log is energy that was really spent.
    """
    present = {step_id for breakdown in breakdowns if breakdown for step_id in breakdown}
    ordered = [step_id for step_id in declared if step_id in present]
    return ordered + sorted(present - set(ordered))


def _step_deltas(
    prepared_a: "_PreparedVersion", prepared_b: "_PreparedVersion", ordered: list[str]
) -> list[tuple[str, float]]:
    """`(step_id, delta)` per step, in scenario order rather than by magnitude."""
    series_a = _version_step_series(prepared_a)
    series_b = _version_step_series(prepared_b)
    return [
        (
            step_id,
            float(np.mean(series_b.get(step_id) or [0.0]))
            - float(np.mean(series_a.get(step_id) or [0.0])),
        )
        for step_id in ordered
    ]


def _iteration_summary(
    prepared_a: "_PreparedVersion", prepared_b: "_PreparedVersion"
) -> dict | None:
    """Mean and SD per iteration per version, or `None` for a scenario with no repeated block."""
    series_a = _version_iteration_series(prepared_a)
    series_b = _version_iteration_series(prepared_b)
    iterations = sorted(set(series_a) & set(series_b))
    if not iterations:
        return None
    return {
        "iterations": iterations,
        "a": {str(i): _mean_sd(series_a[i]) for i in iterations},
        "b": {str(i): _mean_sd(series_b[i]) for i in iterations},
    }


def _version_container_series(prepared: "_PreparedVersion") -> dict[str, list[float]]:
    """Per-role energy as one value per repetition, zero-filled for roles absent from a given
    repetition, so every role's series has the same length and can be compared."""
    per_repetition = [
        breakdown
        for rep in prepared.ok_repetitions
        if (breakdown := _repetition_container_energy(rep)) is not None
    ]
    if not per_repetition:
        return {}
    roles = {role for breakdown in per_repetition for role in breakdown}
    return {role: [breakdown.get(role, 0.0) for breakdown in per_repetition] for role in roles}


def _version_container_breakdown(prepared: "_PreparedVersion") -> dict[str, float] | None:
    """Mean per-repetition energy per container role, averaged over whichever OK
    repetitions actually have an archived `container_map.json`; `None` if none do,
    e.g. every repetition predates that addition."""
    series = _version_container_series(prepared)
    return {role: float(np.mean(values)) for role, values in series.items()} or None


def _version_thread_series(prepared: "_PreparedVersion", role: str) -> dict[str, list[float]]:
    """Per-thread energy within `role`, one value per repetition, zero-filled as above."""
    per_repetition = [
        _parse_thread_energy(*export, role)
        for rep in prepared.ok_repetitions
        if (export := _repetition_export(rep)) is not None
    ]
    if not per_repetition:
        return {}
    names = {name for breakdown in per_repetition for name in breakdown}
    return {name: [breakdown.get(name, 0.0) for breakdown in per_repetition] for name in names}


def _significant_with_other(totals: dict[str, float]) -> dict[str, float]:
    """Entries worth their own bar, with everything else summed into one bucket.

    Used for the thread breakdown, where the grouping key cannot be fully trusted
    (`_thread_name`). A share threshold rather than a fixed count is what actually separates
    real thread groups from the spurious variants a corrupted `comm` produces, since those are
    always tiny: a fixed top-N keeps them as their own bars whenever a container happens to have
    few real groups. `THREAD_BREAKDOWN_MAX_GROUPS` then caps the result so a container with many
    genuinely significant thread groups still produces a readable figure.
    """
    grand_total = sum(totals.values())
    if not grand_total:
        return dict(totals)
    ranked = sorted(totals.items(), key=lambda item: -item[1])
    kept = [
        (name, value)
        for name, value in ranked[:THREAD_BREAKDOWN_MAX_GROUPS]
        if value / grand_total >= THREAD_MIN_SHARE
    ]
    result = dict(kept)
    remainder = grand_total - sum(result.values())
    if remainder > 0:
        result[THREAD_OTHER_LABEL] = remainder
    return result


def _version_energy_split(prepared: "_PreparedVersion") -> dict | None:
    """Mean active and idle energy per repetition, plus the active share of the total."""
    splits = [
        split
        for rep in prepared.ok_repetitions
        if (split := _repetition_energy_split(rep)) is not None
    ]
    if not splits:
        return None
    active = [split.active_j for split in splits]
    idle = [split.idle_j for split in splits]
    total_mean = float(np.mean([split.total_j for split in splits]))
    return {
        "n_repetitions": len(splits),
        "active_j": _mean_sd(active),
        "idle_j": _mean_sd(idle),
        "active_share": (float(np.mean(active)) / total_mean) if total_mean else None,
        "reconciles": all(split.reconciles for split in splits),
    }


def _combined_role_energy(*breakdowns: dict[str, float] | None) -> dict[str, float]:
    """Total energy per container role across both versions, the ranking basis for which
    containers the thread breakdown covers. Chosen by measurement rather than named in code, so
    it targets whatever containers actually dominate a given target application."""
    combined: dict[str, float] = {}
    for breakdown in breakdowns:
        for role, value in (breakdown or {}).items():
            combined[role] = combined.get(role, 0.0) + value
    return combined


def _top_roles(*breakdowns: dict[str, float] | None, limit: int) -> list[str]:
    """The `limit` highest-energy container roles, descending."""
    combined = _combined_role_energy(*breakdowns)
    return sorted(combined, key=lambda role: -combined[role])[:limit]


def _mean_sd(values: list[float]) -> dict | None:
    """n, mean and SD of `values`; `None` for an empty list."""
    if not values:
        return None
    array = np.asarray(values, dtype=float)
    return {
        "n": len(array),
        "mean": float(array.mean()),
        "sd": float(array.std(ddof=1)) if len(array) > 1 else None,
    }


def _t_ci_mean(values: list[float]) -> list[float]:
    """95% t-interval for the mean of `values`."""
    if len(values) < 2:
        value = float(values[0]) if values else float("nan")
        return [value, value]
    array = np.asarray(values, dtype=float)
    low, high = stats.t.interval(
        0.95, len(array) - 1, loc=float(np.mean(array)), scale=float(stats.sem(array))
    )
    return [float(low), float(high)]


def _welch_ci_difference(baseline: list[float], other: list[float]) -> list[float]:
    """95% Welch interval for `mean(other) - mean(baseline)`."""
    if len(baseline) < 2 or len(other) < 2:
        difference = float(np.mean(other)) - float(np.mean(baseline))
        return [difference, difference]
    interval = stats.ttest_ind(other, baseline, equal_var=False).confidence_interval(0.95)
    return [float(interval.low), float(interval.high)]


def _describe(values: list[float]) -> dict:
    """n, mean, SD, median, min and max of one version's values."""
    array = np.asarray(values, dtype=float)
    if not len(array):
        return {"n": 0, "mean": None, "sd": None, "median": None, "min": None, "max": None}
    return {
        "n": len(array),
        "mean": float(np.mean(array)),
        "sd": float(np.std(array, ddof=1)) if len(array) > 1 else None,
        "median": float(np.median(array)),
        "min": float(np.min(array)),
        "max": float(np.max(array)),
    }


def _welch_df(baseline: np.ndarray, other: np.ndarray) -> float:
    """Welch-Satterthwaite degrees of freedom for `mean(other) - mean(baseline)`."""
    var_a = np.var(baseline, ddof=1) / len(baseline)
    var_b = np.var(other, ddof=1) / len(other)
    return float(
        (var_a + var_b) ** 2 / (var_a**2 / (len(baseline) - 1) + var_b**2 / (len(other) - 1))
    )


def _fieller_ci_percent(baseline: list[float], other: list[float]) -> list[float] | None:
    """95% Fieller interval for `mean(other) / mean(baseline) - 1`, in percent.

    Independent samples with unequal variances, using the Welch-Satterthwaite degrees of
    freedom. `None` when the baseline mean is not clearly nonzero, where the interval is
    unbounded.
    """
    a, b = np.asarray(baseline, dtype=float), np.asarray(other, dtype=float)
    mean_a, mean_b = float(np.mean(a)), float(np.mean(b))
    var_a = float(np.var(a, ddof=1)) / len(a)
    var_b = float(np.var(b, ddof=1)) / len(b)
    t = float(stats.t.ppf(0.975, _welch_df(a, b)))
    # Roots of (mean_b - r * mean_a)^2 = t^2 * (var_b + r^2 * var_a) in the ratio r.
    quad = mean_a**2 - t**2 * var_a
    if quad <= 0:
        return None
    half_width = math.sqrt((mean_a * mean_b) ** 2 - quad * (mean_b**2 - t**2 * var_b))
    low, high = (mean_a * mean_b - half_width) / quad, (mean_a * mean_b + half_width) / quad
    return [(low - 1) * 100, (high - 1) * 100]


def _compare(baseline: list[float], other: list[float]) -> dict | None:
    """Version b against the baseline a: the difference of means and its tests.

    Welch's t-test with its 95% CI, the relative difference with a Fieller 95% CI, and the
    Mann-Whitney U p-value as a robustness check. `None` with fewer than two values per side.
    """
    if len(baseline) < 2 or len(other) < 2:
        return None
    welch = stats.ttest_ind(other, baseline, equal_var=False)
    interval = welch.confidence_interval(0.95)
    mean_a, mean_b = float(np.mean(baseline)), float(np.mean(other))
    return {
        "difference_j": mean_b - mean_a,
        "welch": {
            "t": float(welch.statistic),
            "df": float(welch.df),
            "p": float(welch.pvalue),
            "ci95_j": [float(interval.low), float(interval.high)],
        },
        "relative_difference": {
            "percent": (mean_b - mean_a) / mean_a * 100 if mean_a else None,
            "ci95_percent": _fieller_ci_percent(baseline, other),
            "method": "fieller",
        },
        "mann_whitney_p": float(
            stats.mannwhitneyu(other, baseline, alternative="two-sided").pvalue
        ),
    }


def _order_adjusted(
    baseline: list[float],
    baseline_slots: list[int],
    other: list[float],
    other_slots: list[int],
) -> dict | None:
    """The difference of means adjusted for run order: OLS of energy on version and slot.

    Reported because the schedule is a random order, not a balanced one, so a drift over the
    session can load more onto one version. `None` with fewer than two values per side.
    """
    if len(baseline) < 2 or len(other) < 2:
        return None
    y = np.asarray([*baseline, *other], dtype=float)
    version = np.asarray([0.0] * len(baseline) + [1.0] * len(other))
    slot = np.asarray([*baseline_slots, *other_slots], dtype=float)
    design = np.column_stack([np.ones_like(y), version, slot])
    coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
    residuals = y - design @ coefficients
    df = len(y) - design.shape[1]
    if df <= 0:
        return None
    covariance = float(residuals @ residuals) / df * np.linalg.inv(design.T @ design)
    standard_error = math.sqrt(covariance[1, 1])
    t = float(stats.t.ppf(0.975, df))
    difference = float(coefficients[1])
    return {
        "difference_j": difference,
        "ci95_j": [difference - t * standard_error, difference + t * standard_error],
    }


def _execution_slots(execution_order: list[int]) -> list[tuple[int, int]]:
    """Slot `i`'s `(app_version_id, repetition_index)`, reconstructed the same way
    `run_session` itself walks `execution_order` (orchestrator.py), since no repetition-to-slot
    mapping is stored directly."""
    counters: dict[int, int] = {}
    slots = []
    for version_id in execution_order:
        index = counters.get(version_id, 0)
        slots.append((version_id, index))
        counters[version_id] = index + 1
    return slots


def _repetition_slots(execution_order: list[int]) -> dict[tuple[int, int], int]:
    """`(app_version_id, repetition_index)` to the slot it ran in."""
    return {key: slot for slot, key in enumerate(_execution_slots(execution_order))}


def _slotted_values(
    prepared: _PreparedVersion, field: str, slots: dict[tuple[int, int], int]
) -> tuple[list[float], list[int]]:
    """A version's values of `field` with the slot each one ran in, skipping unscheduled ones."""
    values, value_slots = [], []
    for rep in prepared.ok_repetitions:
        value = getattr(rep, field)
        slot = slots.get((prepared.app_version_id, rep.repetition_index))
        if value is not None and slot is not None:
            values.append(value)
            value_slots.append(slot)
    return values, value_slots


def _component_comparison(
    prepared_a: _PreparedVersion,
    prepared_b: _PreparedVersion,
    field: str,
    slots: dict[tuple[int, int], int],
) -> dict | None:
    """`_compare` for one energy component, plus its run-order-adjusted difference."""
    values_a = _field_values(prepared_a.ok_repetitions, field)
    values_b = _field_values(prepared_b.ok_repetitions, field)
    comparison = _compare(values_a, values_b)
    if comparison is None:
        return None
    comparison["order_adjusted"] = _order_adjusted(
        *_slotted_values(prepared_a, field, slots), *_slotted_values(prepared_b, field, slots)
    )
    return comparison


def _role_deltas(
    prepared_a: _PreparedVersion, prepared_b: _PreparedVersion
) -> list[tuple[str, float, list[float]]]:
    """`(role, delta, 95% Welch CI)` per container role, ascending by delta."""
    series_a = _version_container_series(prepared_a)
    series_b = _version_container_series(prepared_b)
    roles = sorted({*series_a, *series_b})
    deltas = []
    for role in roles:
        values_a = series_a.get(role) or [0.0]
        values_b = series_b.get(role) or [0.0]
        delta = float(np.mean(values_b)) - float(np.mean(values_a))
        deltas.append((role, delta, _welch_ci_difference(values_a, values_b)))
    return sorted(deltas, key=lambda item: item[1])


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _version_summary(prepared: _PreparedVersion, n_scheduled: int) -> dict:
    return {
        "app_version_id": prepared.app_version_id,
        "label": prepared.label,
        "n_scheduled": n_scheduled,
        "n_ok": len(prepared.ok_repetitions),
        "n_retried": prepared.n_retried,
        "n_noted": prepared.n_noted,
        "started_at": _iso(prepared.run_started_at),
        "ended_at": _iso(prepared.run_ended_at),
        "cpu_energy_j": _describe(prepared.cpu_values),
        "dram_energy_j": _describe(prepared.dram_values) if prepared.dram_values else None,
        "runtime_s": _mean_sd(prepared.runtime_values),
        "container_breakdown": _version_container_breakdown(prepared),
        "step_breakdown": _version_step_breakdown(prepared),
        "energy_split": _version_energy_split(prepared),
    }


def _repetition_records(
    prepared: _PreparedVersion, key: str, slots: dict[tuple[int, int], int]
) -> list[dict]:
    """One record per usable repetition, so the per-repetition values can be reused directly."""
    return [
        {
            "slot": slots.get((prepared.app_version_id, rep.repetition_index)),
            "version": key,
            "repetition_index": rep.repetition_index,
            "cpu_energy_j": rep.cpu_energy_j,
            "dram_energy_j": rep.dram_energy_j,
            "runtime_s": rep.runtime_s,
            # Repetition start (before the stack reset) and the declared-step span, all UTC.
            "measurement_started_at": _iso(rep.measurement_started_at),
            "start_time": _iso(rep.start_time),
            "end_time": _iso(rep.end_time),
            "attempt_count": rep.attempt_count,
            "validation_notes": rep.validation_notes,
        }
        for rep in prepared.ok_repetitions
    ]


def _find_equivalence_check(
    session: Session, comparison_session: ComparisonSession
) -> EquivalenceCheck | None:
    """The `equivalence_check` row `run_preflight_gate` inserted for this session's pair,
    matched by scenario/version pair and timing rather than a stored FK, since none exists."""
    version_ids = [vr.app_version_id for vr in comparison_session.version_runs]
    if len(version_ids) != 2:
        return None
    id_x, id_y = version_ids
    return (
        session.query(EquivalenceCheck)
        .filter(
            EquivalenceCheck.scenario_id == comparison_session.scenario_id,
            or_(
                and_(EquivalenceCheck.version_a_id == id_x, EquivalenceCheck.version_b_id == id_y),
                and_(EquivalenceCheck.version_a_id == id_y, EquivalenceCheck.version_b_id == id_x),
            ),
            EquivalenceCheck.checked_at <= comparison_session.calibration_timestamp,
        )
        .order_by(EquivalenceCheck.checked_at.desc())
        .first()
    )


def build_summary(
    comparison_session: ComparisonSession,
    equivalence_check: EquivalenceCheck,
    prepared_a: _PreparedVersion,
    prepared_b: _PreparedVersion,
) -> dict:
    """The exact `summary.json` structure; `comparison_report.md` renders from this, never
    recomputes anything independently."""
    # `scenario` is a required FK, so an unreachable one is a real fault. Tolerating it would
    # silently change the step order the report and figures claim is the scenario's.
    scenario = comparison_session.scenario
    if scenario is None:
        raise EvaluationError(
            f"comparison_session {comparison_session.id} has no reachable scenario row "
            f"(scenario_id={comparison_session.scenario_id}); refusing to build a summary whose "
            "step order would silently not be the scenario's"
        )
    execution_order = comparison_session.execution_order or []
    slots = _repetition_slots(execution_order)
    scheduled = Counter(execution_order)
    version_a = _version_summary(prepared_a, scheduled.get(prepared_a.app_version_id, 0))
    version_b = _version_summary(prepared_b, scheduled.get(prepared_b.app_version_id, 0))
    step_order = _ordered_steps(
        list(scenario.steps or []), version_a["step_breakdown"], version_b["step_breakdown"]
    )
    has_dram = bool(prepared_a.dram_values and prepared_b.dram_values)
    return {
        "scenario_id": comparison_session.scenario_id,
        "comparison_session_id": comparison_session.id,
        "generated_at": datetime.now(UTC).isoformat(),
        "session_context": {
            "execution_order": execution_order,
            "random_seed": comparison_session.random_seed,
            "idle_power_calibration": comparison_session.idle_power_calibration,
            "host_descriptor": comparison_session.host_descriptor,
            "metrion_config_path": comparison_session.metrion_config_path,
            "cpu_governor": comparison_session.cpu_governor,
            "host_facts": comparison_session.host_facts or {},
        },
        "equivalence_check": {
            "verdict": equivalence_check.verdict.value,
            "notes": equivalence_check.notes,
            "checked_at": (
                equivalence_check.checked_at.isoformat() if equivalence_check.checked_at else None
            ),
        },
        # k as declared: a headline number is energy per scenario execution of k iterations.
        "scenario_iterations": scenario.iterations,
        "step_order": step_order,
        "versions": {"a": version_a, "b": version_b},
        "comparison": {
            "baseline": "a",
            "cpu_energy_j": _component_comparison(prepared_a, prepared_b, "cpu_energy_j", slots),
            "dram_energy_j": (
                _component_comparison(prepared_a, prepared_b, "dram_energy_j", slots)
                if has_dram
                else None
            ),
            "container_deltas": [
                {"role": role, "delta_j": delta, "welch_ci95_j": interval}
                for role, delta, interval in reversed(_role_deltas(prepared_a, prepared_b))
            ],
            "step_deltas": [
                {"step_id": step_id, "delta_j": delta}
                for step_id, delta in _step_deltas(prepared_a, prepared_b, step_order)
            ],
        },
        "per_iteration": _iteration_summary(prepared_a, prepared_b),
        "repetitions": sorted(
            _repetition_records(prepared_a, "a", slots)
            + _repetition_records(prepared_b, "b", slots),
            key=lambda record: (record["slot"] is None, record["slot"]),
        ),
    }


def _energy_panels(
    prepared_a: _PreparedVersion, prepared_b: _PreparedVersion
) -> list[tuple[str, str, str]]:
    """`(component, prepared attribute, `Repetition` field)` for every energy component both
    versions actually measured.

    Every energy figure derives its panel list from here rather than hardcoding CPU, so DRAM
    appears consistently across all of them on a host that has a DRAM RAPL domain and is
    consistently absent on one that does not.
    """
    panels = [("CPU energy", "cpu_values", "cpu_energy_j")]
    if prepared_a.dram_values and prepared_b.dram_values:
        panels.append(("DRAM energy", "dram_values", "dram_energy_j"))
    return panels


def _tick_label(prepared: _PreparedVersion, values_attr: str) -> str:
    """An `app_version`'s label, wrapped to fit under one bar, carrying its own sample size.

    The wrap matters because real `app_version` labels are long enough to collide between
    adjacent bars at these figure widths. The count matters because the two versions can differ
    (a failed repetition drops one side), which a reader otherwise cannot see from the figure,
    and it names "repetitions" rather than a bare `n` so there is no ambiguity about what is
    being counted.
    """
    count = len(getattr(prepared, values_attr))
    unit = "repetition" if count == 1 else "repetitions"
    # `break_long_words` (on by default) keeps the width bounded even for a label with no
    # whitespace to wrap at, so a long label grows the figure taller rather than overflowing
    # into its neighbour. An empty or whitespace-only label wraps to nothing, which would leave
    # both bars labelled only by their repetition count and therefore indistinguishable, so it
    # falls back to the `app_version` id: `label` is `nullable=False` but "" satisfies that.
    lines = textwrap.wrap(prepared.label, TICK_LABEL_WRAP_WIDTH) or [
        f"app_version {prepared.app_version_id}"
    ]
    return "\n".join([*lines, f"({count} {unit})"])


def _shared_label_prefix(label_a: str, label_b: str) -> tuple[str, str, str]:
    """`(shared, rest_a, rest_b)`: the leading words both labels share, and what remains.

    Each remainder keeps at least one word; with no shared words the labels come back whole.
    """
    words_a, words_b = label_a.split(), label_b.split()
    shared = 0
    while shared < min(len(words_a), len(words_b)) - 1 and words_a[shared] == words_b[shared]:
        shared += 1
    if shared == 0:
        return "", label_a, label_b
    return (
        " ".join(words_a[:shared]),
        " ".join(words_a[shared:]),
        " ".join(words_b[shared:]),
    )


def _figure_versions(
    prepared_a: _PreparedVersion, prepared_b: _PreparedVersion
) -> tuple[_PreparedVersion, _PreparedVersion]:
    """Copies of both versions carrying only their distinguishing label words, for figures."""
    shared, rest_a, rest_b = _shared_label_prefix(prepared_a.label, prepared_b.label)
    return (
        replace(prepared_a, label=rest_a, label_context=shared),
        replace(prepared_b, label=rest_b, label_context=shared),
    )


def _save(fig, path: Path, context: str = "") -> None:
    """`bbox_inches="tight"` so legends placed outside the axes are not cropped away.

    Every figure is also written as a PDF sibling, for dropping into a LaTeX write-up as
    vector graphics rather than a fixed-resolution raster; `FIGURE_DPI` only matters for the PNG,
    since the PDF is vector. Not listed in `figure_relpaths`/`summary.json`: same figure, second
    encoding, not new information a consumer couldn't derive by swapping the extension.
    """
    if context:
        # Placed above everything already drawn, so it never collides with an axes title.
        bbox = fig.get_tightbbox(fig.canvas.get_renderer())
        fig.text(
            (bbox.x0 + bbox.x1) / 2 / fig.get_figwidth(),
            bbox.y1 / fig.get_figheight() + 0.01,
            context,
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )
    fig.savefig(path, dpi=FIGURE_DPI, bbox_inches="tight")
    fig.savefig(path.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)


def _figure_bar_chart(
    prepared_a: _PreparedVersion, prepared_b: _PreparedVersion, path: Path
) -> None:
    panels = _energy_panels(prepared_a, prepared_b)
    fig, axes = plt.subplots(1, len(panels), figsize=(5.5 * len(panels), 4.5), squeeze=False)
    for ax, (component, attr, _) in zip(axes[0], panels, strict=True):
        values = [getattr(prepared_a, attr), getattr(prepared_b, attr)]
        means = [float(np.mean(v)) for v in values]
        cis = [_t_ci_mean(v) for v in values]
        errors = [[mean - lo, hi - mean] for mean, (lo, hi) in zip(means, cis, strict=True)]
        ax.bar(
            [_tick_label(prepared_a, attr), _tick_label(prepared_b, attr)],
            means,
            yerr=np.array(errors).T,
            capsize=5,
        )
        difference = means[1] - means[0]
        percent = difference / means[0] * 100 if means[0] else float("nan")
        ax.set_ylabel(f"Mean {component} per repetition (J)")
        # Titled per panel, not once on the first axis, so the DRAM panel is not left untitled.
        # The difference is in the title because a few-percent gap is invisible on a zero-based
        # bar.
        ax.set_title(
            f"{component}: mean per repetition\n(error bars: 95% CI of the mean)\n"
            f"right minus left: {difference:+.1f} J ({percent:+.1f}%)"
        )
    _save(fig, path, prepared_a.label_context)


def _figure_distribution(
    prepared_a: _PreparedVersion, prepared_b: _PreparedVersion, path: Path
) -> None:
    panels = _energy_panels(prepared_a, prepared_b)
    fig, axes = plt.subplots(1, len(panels), figsize=(5.5 * len(panels), 4.5), squeeze=False)
    positions = [1, 2]
    for ax, (component, attr, _) in zip(axes[0], panels, strict=True):
        data = [getattr(prepared_a, attr), getattr(prepared_b, attr)]
        ax.violinplot(data, positions=positions, showmeans=False, showextrema=False)
        ax.boxplot(data, positions=positions, widths=0.15, showfliers=False)
        # Every repetition as a point; a fixed seed keeps the jitter identical between runs.
        jitter = np.random.default_rng(0)
        for position, values in zip(positions, data, strict=True):
            xs = position + jitter.uniform(-0.06, 0.06, len(values))
            ax.scatter(xs, values, s=10, color="black", alpha=0.6, zorder=3)
        ax.set_xticks(positions)
        ax.set_xticklabels([_tick_label(prepared_a, attr), _tick_label(prepared_b, attr)])
        ax.set_ylabel(f"Total {component} per repetition (J)")
        ax.set_title(f"{component} distribution over repetitions")
    _save(fig, path, prepared_a.label_context)


def _figure_run_order_trend(
    execution_order: list[int],
    version_a_id: int,
    prepared_a: _PreparedVersion,
    version_b_id: int,
    prepared_b: _PreparedVersion,
    path: Path,
) -> None:
    """Energy against execution-order slot, with a least-squares fit per version.

    The fit is the point of the figure: it shows whether energy drifts over the session, which
    the slope and r2 state where a bare scatter only invites the reader to eyeball it.
    Deliberately a fitted line rather than joining consecutive points, which would plot noise.
    """
    slots = _execution_slots(execution_order)
    panels = _energy_panels(prepared_a, prepared_b)
    fig, axes = plt.subplots(len(panels), 1, figsize=(9, 4.0 * len(panels)), squeeze=False)
    for ax, (component, _, field) in zip(axes[:, 0], panels, strict=True):
        for version_id, prepared in ((version_a_id, prepared_a), (version_b_id, prepared_b)):
            energy_by_index = {
                rep.repetition_index: getattr(rep, field)
                for rep in prepared.ok_repetitions
                if getattr(rep, field) is not None
            }
            points = [
                (slot, energy_by_index[index])
                for slot, (vid, index) in enumerate(slots)
                if vid == version_id and index in energy_by_index
            ]
            if not points:
                continue
            xs = [x for x, _ in points]
            ys = [y for _, y in points]
            label = prepared.label
            fit = stats.linregress(xs, ys) if len(points) >= 2 else None
            if fit is not None:
                # r-squared alongside the slope so the line cannot be read as drift on its own.
                # A visually convincing tilt routinely explains almost none of the variance,
                # and this figure exists to let a reader check that rather than eyeball it.
                label = (
                    f"{prepared.label} (trend {fit.slope:+.4f} J/slot, r2 = {fit.rvalue**2:.3f})"
                )
            scatter = ax.scatter(xs, ys, label=label)
            if fit is not None:
                line_x = np.array([min(xs), max(xs)])
                ax.plot(
                    line_x,
                    fit.slope * line_x + fit.intercept,
                    color=scatter.get_facecolor()[0],
                )
        ax.set_xlabel("Execution order (slot)")
        ax.set_ylabel(f"{component} per repetition (J)")
        ax.set_title(f"{component} per repetition against execution order")
        ax.legend()
    _save(fig, path, prepared_a.label_context)


def _figure_container_breakdown(
    prepared_a: _PreparedVersion, prepared_b: _PreparedVersion, path: Path
) -> None:
    breakdown_a = _version_container_breakdown(prepared_a) or {}
    breakdown_b = _version_container_breakdown(prepared_b) or {}
    roles = sorted({*breakdown_a, *breakdown_b})
    labels = [_tick_label(prepared_a, "cpu_values"), _tick_label(prepared_b, "cpu_values")]
    totals = [sum(breakdown_a.values()), sum(breakdown_b.values())]
    # Same interval as fig1, on the version's own `cpu_values`.
    cis = [_t_ci_mean(prepared_a.cpu_values), _t_ci_mean(prepared_b.cpu_values)]
    errors = [[total - lo, hi - total] for total, (lo, hi) in zip(totals, cis, strict=True)]

    fig, ax = plt.subplots(figsize=(7, 5))
    bottom = [0.0, 0.0]
    for role in roles:
        values = [breakdown_a.get(role, 0.0), breakdown_b.get(role, 0.0)]
        bars = ax.bar(labels, values, bottom=bottom, label=role)
        # Value inside each segment, but only where the segment is tall enough to hold text.
        # The two stacks are near-identical by construction (a small delta on a large base), so
        # the numbers carry the comparison that the segment heights cannot.
        ax.bar_label(
            bars,
            labels=[
                f"{value:.1f} J" if total and value / total >= CONTAINER_LABEL_MIN_SHARE else ""
                for value, total in zip(values, totals, strict=True)
            ],
            label_type="center",
            fontsize=8,
            color="white",
        )
        bottom = [b + v for b, v in zip(bottom, values, strict=True)]
    ax.errorbar(labels, totals, yerr=np.array(errors).T, fmt="none", ecolor="black", capsize=5)
    for x, (total, (_, hi)) in enumerate(zip(totals, cis, strict=True)):
        # Above the CI whisker rather than the bar top, since the whisker is the highest thing
        # on the bar and a label at the bar top would sit inside it.
        ax.annotate(
            f"total {total:.1f} J",
            (x, hi),
            textcoords="offset points",
            xytext=(0, 4),
            ha="center",
            fontsize=8,
        )
    ax.set_ylabel("Mean CPU energy per repetition (J)")
    ax.set_title(
        "Per-container CPU energy breakdown\n(mean per repetition; error bars: 95% CI of the "
        "mean total)"
    )
    # Outside the axes; the previous in-axes placement covered the bars it described.
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), title="Container", fontsize=8)
    _save(fig, path, prepared_a.label_context)


def _figure_role_deltas(
    prepared_a: _PreparedVersion, prepared_b: _PreparedVersion, path: Path
) -> None:
    """Signed per-container delta with an unadjusted 95% Welch CI on each, against the total.

    Shows what the stacked breakdown cannot: the stacks differ by a few percent on a large
    shared base, so one container moving against the others is invisible there.
    """
    deltas = _role_deltas(prepared_a, prepared_b)
    if not deltas:
        return
    roles = [role for role, _, _ in deltas]
    values = [delta for _, delta, _ in deltas]
    errors = [[delta - low, high - delta] for (_, delta, (low, high)) in deltas]
    total_delta = float(np.mean(prepared_b.cpu_values)) - float(np.mean(prepared_a.cpu_values))

    fig, ax = plt.subplots(figsize=(8, 0.7 * len(roles) + 3))
    ax.barh(roles, values, xerr=np.array(errors).T, capsize=4, color="tab:blue")
    ax.axvline(0, color="black", linewidth=1)
    ax.axvline(
        total_delta,
        color="tab:red",
        linestyle="--",
        linewidth=1.2,
        label=f"total across all containers ({total_delta:+.3f} J)",
    )
    for y, (_, delta, (low, high)) in enumerate(deltas):
        # Past the whisker's outer end, so the text does not sit on the whisker.
        ax.annotate(
            f"{delta:+.3f} J",
            (high if delta >= 0 else low, y),
            textcoords="offset points",
            xytext=(10 if delta >= 0 else -10, 0),
            ha="left" if delta >= 0 else "right",
            va="center",
            fontsize=8,
        )
    ax.set_xlabel(
        f"Change in mean CPU energy per repetition, {prepared_b.label} minus {prepared_a.label} (J)"
    )
    ax.set_title(
        f"Per-container change in CPU energy\n(error bars: unadjusted 95% Welch CI, "
        f"{len(roles)} containers)"
    )
    ax.margins(x=0.25)
    ax.legend(loc="lower right", fontsize=8)
    _save(fig, path, prepared_a.label_context)


def _figure_step_breakdown(
    prepared_a: _PreparedVersion,
    prepared_b: _PreparedVersion,
    ordered: list[str],
    path: Path,
) -> None:
    """Per-step composition, stacked, in scenario order.

    Follows the per-container breakdown's grammar: stacked bars with in-segment `J` labels and
    the total annotated above. No interval: the step split is energy measured during each step,
    not caused by it. Two things differ, both because steps are ordered and
    containers are not. Segments run in scenario order rather than sorted, so the stack reads
    bottom-to-top as the scenario ran. And under a k-iteration scenario each segment is a step id
    aggregated across its iterations, with the per-iteration view left to a separate figure, so
    each answers one question.

    Composition alone would mislead, which is why the per-step change figure exists beside it: a
    step can dominate the total energy while contributing almost nothing to the actual
    difference, or the reverse, and composition read alone can invert the conclusion.
    """
    breakdown_a = _version_step_breakdown(prepared_a) or {}
    breakdown_b = _version_step_breakdown(prepared_b) or {}
    if not breakdown_a and not breakdown_b:
        return
    labels = [_tick_label(prepared_a, "cpu_values"), _tick_label(prepared_b, "cpu_values")]
    totals = [sum(breakdown_a.values()), sum(breakdown_b.values())]

    fig, ax = plt.subplots(figsize=(7, 5))
    bottom = [0.0, 0.0]
    for step_id in ordered:
        values = [breakdown_a.get(step_id, 0.0), breakdown_b.get(step_id, 0.0)]
        bars = ax.bar(labels, values, bottom=bottom, label=step_id)
        ax.bar_label(
            bars,
            labels=[
                f"{value:.1f} J" if total and value / total >= CONTAINER_LABEL_MIN_SHARE else ""
                for value, total in zip(values, totals, strict=True)
            ],
            label_type="center",
            fontsize=8,
            color="white",
        )
        bottom = [b + v for b, v in zip(bottom, values, strict=True)]
    for x, total in enumerate(totals):
        ax.annotate(
            f"total {total:.1f} J",
            (x, total),
            textcoords="offset points",
            xytext=(0, 4),
            ha="center",
            fontsize=8,
        )
    ax.set_ylabel("Mean CPU energy per repetition (J)")
    ax.set_title(
        "Per-step CPU energy breakdown\n(scenario order; mean per repetition; measured during "
        "each step)"
    )
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), title="Step", fontsize=8)
    _save(fig, path, prepared_a.label_context)


def _figure_step_deltas(
    prepared_a: _PreparedVersion,
    prepared_b: _PreparedVersion,
    ordered: list[str],
    path: Path,
) -> None:
    """Signed per-step change in mean energy, in scenario order, against the total.

    No intervals: the background worker queue shifts work between steps, so these differences
    describe when energy was measured, not what each step caused.
    """
    deltas = _step_deltas(prepared_a, prepared_b, ordered)
    if not deltas:
        return
    # Top-to-bottom in scenario order, which `barh` reverses without this.
    deltas = list(reversed(deltas))
    step_ids = [step_id for step_id, _ in deltas]
    values = [delta for _, delta in deltas]
    total_delta = sum(values)

    fig, ax = plt.subplots(figsize=(8, 0.7 * len(step_ids) + 3))
    ax.barh(step_ids, values, color="tab:blue")
    ax.axvline(0, color="black", linewidth=1)
    ax.axvline(
        total_delta,
        color="tab:red",
        linestyle="--",
        linewidth=1.2,
        label=f"total across all steps ({total_delta:+.3f} J)",
    )
    for y, delta in enumerate(values):
        ax.annotate(
            f"{delta:+.3f} J",
            (delta, y),
            textcoords="offset points",
            xytext=(10 if delta >= 0 else -10, 0),
            ha="left" if delta >= 0 else "right",
            va="center",
            fontsize=8,
        )
    ax.set_xlabel(
        f"Change in mean CPU energy per repetition, {prepared_b.label} minus {prepared_a.label} (J)"
    )
    ax.set_title(
        "Per-step change in CPU energy measured during each step\n(scenario order; includes work "
        "shifted between steps)"
    )
    ax.margins(x=0.25)
    ax.legend(loc="lower right", fontsize=8)
    _save(fig, path, prepared_a.label_context)


def _figure_iteration_trend(
    prepared_a: _PreparedVersion,
    prepared_b: _PreparedVersion,
    path: Path,
) -> None:
    """Mean energy per iteration per version, with the SD across repetitions as whiskers.

    Points are joined for readability, not as a claim about what happens between them. No
    per-iteration difference is drawn: the background worker queue shifts work between
    iterations, so such differences describe timing rather than an effect.
    """
    series_a = _version_iteration_series(prepared_a)
    series_b = _version_iteration_series(prepared_b)
    iterations = sorted(set(series_a) & set(series_b))
    if len(iterations) < 2:
        return

    fig, ax = plt.subplots(figsize=(8, 4.5))
    # The two versions are offset slightly on x so their whiskers do not overlap.
    for prepared, series, colour, offset in (
        (prepared_a, series_a, "tab:blue", -0.04),
        (prepared_b, series_b, "tab:orange", 0.04),
    ):
        means = [float(np.mean(series[i])) for i in iterations]
        sds = [float(np.std(series[i], ddof=1)) if len(series[i]) > 1 else 0.0 for i in iterations]
        xs = [i + offset for i in iterations]
        ax.errorbar(
            xs,
            means,
            yerr=sds,
            marker="o",
            capsize=4,
            color=colour,
            label=f"{prepared.label} (n = {len(series[iterations[0]])})",
        )
        for x, mean in zip(xs, means, strict=True):
            ax.annotate(
                f"{mean:.2f} J",
                (x, mean),
                textcoords="offset points",
                xytext=(-8 if offset < 0 else 8, 0),
                ha="right" if offset < 0 else "left",
                va="center",
                fontsize=8,
            )
    ax.set_xlabel("Iteration within one repetition")
    ax.set_ylabel("Mean CPU energy per iteration (J)")
    ax.set_xticks(iterations)
    ax.set_title(
        "Per-iteration CPU energy\n(mean per repetition; error bars: SD across repetitions)"
    )
    ax.margins(x=0.15)
    # Below the axes, since the whiskers can reach any corner inside them.
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15), fontsize=8)
    _save(fig, path, prepared_a.label_context)


def _figure_energy_split(
    prepared_a: _PreparedVersion, prepared_b: _PreparedVersion, path: Path
) -> None:
    """METRION's active/idle decomposition per version, side by side.

    Active energy is attributed by a thread's actual resource usage and idle energy by its time
    share on the component, so the two are driven by different mechanisms
    and can move by different amounts. Reporting only their sum, as the headline
    `cpu_energy_j` does, discards that separation even though METRION already computes it.
    """
    split_a = _version_energy_split(prepared_a)
    split_b = _version_energy_split(prepared_b)
    if not split_a or not split_b:
        return

    components = ["Active", "Idle"]
    means_a = [split_a["active_j"]["mean"], split_a["idle_j"]["mean"]]
    means_b = [split_b["active_j"]["mean"], split_b["idle_j"]["mean"]]
    errors_a = [split_a["active_j"]["sd"], split_a["idle_j"]["sd"]]
    errors_b = [split_b["active_j"]["sd"], split_b["idle_j"]["sd"]]

    x = np.arange(len(components))
    width = 0.36
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for offset, means, errors, prepared, share in (
        (-width / 2, means_a, errors_a, prepared_a, split_a["active_share"]),
        (width / 2, means_b, errors_b, prepared_b, split_b["active_share"]),
    ):
        label = prepared.label
        if share is not None:
            label = f"{prepared.label} ({share * 100:.1f}% active)"
        bars = ax.bar(x + offset, means, width, yerr=errors, capsize=4, label=label)
        ax.bar_label(bars, fmt="%.2f J", fontsize=8, padding=2)
    ax.set_xticks(x)
    ax.set_xticklabels(components)
    ax.set_ylabel("Mean CPU energy per repetition (J)")
    ax.set_title("Active and idle CPU energy per version\n(error bars: SD across repetitions)")
    ax.legend(loc="lower right", fontsize=8)
    _save(fig, path, prepared_a.label_context)


def _thread_panel(
    ax, prepared_a: _PreparedVersion, prepared_b: _PreparedVersion, role: str
) -> bool:
    """Draw one container's thread decomposition onto `ax`. `False` if there is nothing to draw."""
    series_a = _version_thread_series(prepared_a, role)
    series_b = _version_thread_series(prepared_b, role)
    if not series_a or not series_b:
        return False
    means_a = {name: float(np.mean(values)) for name, values in series_a.items()}
    means_b = {name: float(np.mean(values)) for name, values in series_b.items()}
    # Rank on both versions combined so each keeps the same groups and the bars stay comparable.
    combined = {
        name: means_a.get(name, 0.0) + means_b.get(name, 0.0) for name in {*means_a, *means_b}
    }
    names = list(_significant_with_other(combined))

    def regrouped(means: dict[str, float]) -> dict[str, float]:
        kept = {name: means.get(name, 0.0) for name in names if name != THREAD_OTHER_LABEL}
        if THREAD_OTHER_LABEL in names:
            kept[THREAD_OTHER_LABEL] = sum(
                value for name, value in means.items() if name not in kept
            )
        return kept

    grouped_a, grouped_b = regrouped(means_a), regrouped(means_b)
    x = np.arange(len(names))
    width = 0.36
    for offset, grouped, prepared in (
        (-width / 2, grouped_a, prepared_a),
        (width / 2, grouped_b, prepared_b),
    ):
        bars = ax.bar(
            x + offset, [grouped.get(name, 0.0) for name in names], width, label=prepared.label
        )
        ax.bar_label(bars, fmt="%.2f J", fontsize=7, padding=2, rotation=90)
    ax.set_xticks(x)
    # 16, not less: the kernel caps `comm` at 15 characters, so a narrower wrap splits every
    # maximum-length thread name onto a second line for no reason.
    ax.set_xticklabels([textwrap.fill(name, 16) for name in names], fontsize=8)
    ax.set_ylabel("Mean CPU energy\nper repetition (J)", fontsize=9)
    # Each version's own container total goes in the panel title for two reasons. Panels are
    # independently scaled (the dominant container can hold thirty times the energy of a minor
    # one, so a shared y-axis would flatten every panel but the first), and these totals are the
    # scale reference that keeps a tall bar in a minor container's panel from reading as a large
    # absolute contribution. They are also not otherwise recoverable from the figure, since the
    # bars are per-thread. Deliberately the two real per-version totals rather than any figure
    # aggregated across both versions: a sum or share across two configurations is a quantity
    # nothing ever measured, and it contradicts the per-repetition y-axis.
    # Named "container total" so the two numbers cannot be mistaken for one of the per-thread
    # bars below them: they are the sum of every bar in this panel, for each version in turn.
    ax.set_title(
        f"{role}\ncontainer total: {sum(grouped_a.values()):.2f} J "
        f"vs. {sum(grouped_b.values()):.2f} J",
        fontsize=9,
    )
    ax.margins(y=0.22)
    return True


def _figure_thread_breakdown(
    prepared_a: _PreparedVersion,
    prepared_b: _PreparedVersion,
    roles: list[str],
    path: Path,
) -> None:
    """Thread-level decomposition of the highest-energy containers, one panel each.

    The sharpest thing METRION enables and the one a container-level energy tool cannot express
    at all: it turns "this container costs more" into "these threads inside it cost more".
    Covering several containers rather than only the dominant one is what shows that the internal
    thread structure differs between them, and that a container's total moving is not the same
    claim as its threads moving.
    """
    if not roles:
        return
    columns = 1 if len(roles) == 1 else 2
    rows = math.ceil(len(roles) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(6.5 * columns, 4.2 * rows), squeeze=False)
    flat = axes.flatten()
    drawn = 0
    for ax, role in zip(flat, roles, strict=False):
        if _thread_panel(ax, prepared_a, prepared_b, role):
            drawn += 1
        else:
            ax.set_visible(False)
    for ax in flat[len(roles) :]:
        ax.set_visible(False)
    if not drawn:
        plt.close(fig)
        return

    fig.suptitle("Thread-level CPU energy inside the highest-energy containers")
    # Stacked rather than both at "lower center", which drew the label underneath the legend.
    fig.supxlabel("Thread name (kernel comm, truncated to 15 characters)", fontsize=9, y=0.055)
    handles, labels = flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncols=2, fontsize=9, bbox_to_anchor=(0.5, 0))
    # Leave room at the bottom for the shared x-label and legend.
    fig.tight_layout(rect=(0, 0.09, 1, 0.97))
    _save(fig, path, prepared_a.label_context)


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    """Values go into tables rather than into sentences, so this is the report's main primitive."""
    return [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
        *("| " + " | ".join(row) + " |" for row in rows),
        "",
    ]


def _execution_order_block(execution_order: list[int], version_a: dict, version_b: dict) -> str:
    """Renders the order as `a`/`b` letters, since raw `app_version` ids do not say which is which.

    Fixed-width rows rather than one long list: the point of the block is to let a reader see at a
    glance that the two versions really are interleaved, which a 60-element list does not convey.
    """
    letters = {version_a["app_version_id"]: "a", version_b["app_version_id"]: "b"}
    marks = [letters.get(version_id, "?") for version_id in execution_order]
    rows = []
    for start in range(0, len(marks), EXECUTION_ORDER_ROW_WIDTH):
        chunk = marks[start : start + EXECUTION_ORDER_ROW_WIDTH]
        rows.append(f"run {start + 1:>2}-{start + len(chunk):<2}  " + " ".join(chunk))
    return "\n".join(["```", *rows, "```"])


def _host_summary(facts: dict) -> str | None:
    """One line of hardware and OS from `host_facts`, not the whole blob.

    The session-context section is a session's orientation, so it gets the facts a reader
    checks before trusting a number and none of the rest: what ran it, on which kernel and
    distro, and whether it was bare metal. `ram_bytes` and the RAPL domain list are
    deliberately not here; they are in `host_facts` for whoever needs them.
    """
    cpu = facts.get("cpu_model")
    cores = facts.get("cpu_count")
    parts = [
        f"{cpu}" + (f", {cores} cores" if cores else "") if cpu else None,
        f"kernel {facts['kernel_release']}" if facts.get("kernel_release") else None,
        facts.get("distro"),
        "bare metal" if facts.get("virtualization") == "none" else facts.get("virtualization"),
    ]
    return ", ".join(part for part in parts if part) or None


def _metrion_build(facts: dict) -> str | None:
    """METRION's identity, with the hash truncated to a comparable prefix.

    Truncated because the full 64 characters are unreadable in a table and the question the
    line answers is "is this the same build as that other session", which a 16-character
    prefix settles. The whole digest stays in `host_facts` for anyone checking properly.
    """
    version = facts.get("metrion_version")
    digest = facts.get("metrion_package_sha256")
    parts = [
        f"`{version}`" if version else None,
        f"package `{digest[:16]}`" if digest else None,
    ]
    return ", ".join(part for part in parts if part) or None


def _idle_power(calibration: dict[str, float] | None) -> str:
    """Rendered per package rather than as the raw dict repr this used to print."""
    if not calibration:
        return "not calibrated"
    parts = ", ".join(f"{value:.3f} W (`{name}`)" for name, value in sorted(calibration.items()))
    if len(calibration) == 1:
        return parts
    return f"{sum(calibration.values()):.3f} W total: {parts}"


def _num(value: float | None, spec: str = ".3f") -> str:
    """Every numeric cell goes through this, so a metric that is undefined for a given sample
    (an empty one, or a zero mean for the CV) renders as `n/a` rather than as `None`."""
    return format(value, spec) if value is not None else "n/a"


def _p_value(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"< {P_VALUE_FLOOR}" if value < P_VALUE_FLOOR else f"{value:.3f}"


def _interval(bounds: list[float] | None, spec: str = ".2f", unit: str = "") -> str:
    if not bounds:
        return "n/a"
    return f"[{format(bounds[0], '+' + spec)}, {format(bounds[1], '+' + spec)}]{unit}"


def _figure_line(figure_relpaths: dict[str, str], key: str, alt: str) -> list[str]:
    return [f"![{alt}]({figure_relpaths[key]})", ""] if key in figure_relpaths else []


def _render_setup(summary: dict, figure_relpaths: dict[str, str]) -> list[str]:
    ctx = summary["session_context"]
    eq = summary["equivalence_check"]
    version_a, version_b = summary["versions"]["a"], summary["versions"]["b"]
    lines = [
        "## 1. Setup",
        "",
        "Version `a` is the baseline: every difference below is `b` minus `a`, following "
        "`run-session`'s argument order. Only repetitions that reached `ok` are included.",
        "",
    ]
    lines += _table(
        ["", "Version", "`app_version`", "Scheduled", "Usable", "Retried", "With notes"],
        [
            [
                f"Baseline (`{key}`)" if key == "a" else f"Compared (`{key}`)",
                version["label"],
                str(version["app_version_id"]),
                str(version["n_scheduled"]),
                str(version["n_ok"]),
                str(version["n_retried"]),
                str(version["n_noted"]),
            ]
            for key, version in (("a", version_a), ("b", version_b))
        ],
    )
    lines += _table(
        ["Setting", "Value"],
        [
            ["Host", f"`{ctx['host_descriptor']}`"],
            ["METRION config", f"`{ctx['metrion_config_path']}`"],
            ["Measured host idle power", _idle_power(ctx["idle_power_calibration"])],
            # A blank cell would read as "no governor"; the truth is "not recorded".
            [
                "CPU governor",
                f"`{ctx['cpu_governor']}`" if ctx.get("cpu_governor") else "not recorded",
            ],
            ["Host hardware and OS", _host_summary(ctx.get("host_facts") or {}) or "not recorded"],
            ["METRION build", _metrion_build(ctx.get("host_facts") or {}) or "not recorded"],
            ["Random seed", f"`{ctx['random_seed']}`"],
            ["Equivalence check", eq["verdict"] + (f" ({eq['notes']})" if eq["notes"] else "")],
        ],
    )
    lines += [
        "Each repetition's energy covers its declared steps, from the first step's start to the "
        "last step's end, rounded outward to whole seconds; stack reset, readiness wait, browser "
        "launch and login are outside it. CPU energy is METRION's per-thread energy summed over "
        "the target stack's containers, including the idle share METRION attributes to them.",
        "",
        "Execution order (randomized, read left to right):",
        "",
        _execution_order_block(ctx["execution_order"], version_a, version_b),
        "",
    ]
    lines += _figure_line(figure_relpaths, "fig3", "Run-order trend")
    return lines


def _render_descriptives(summary: dict, figure_relpaths: dict[str, str]) -> list[str]:
    version_a, version_b = summary["versions"]["a"], summary["versions"]["b"]
    header = ["Metric", f"`a` {version_a['label']}", f"`b` {version_b['label']}"]
    rows = []
    components = [("CPU energy", "cpu_energy_j")]
    if version_a["dram_energy_j"] and version_b["dram_energy_j"]:
        components.append(("DRAM energy", "dram_energy_j"))
    for name, key in components:
        stats_a, stats_b = version_a[key], version_b[key]
        for field, label, spec in (
            ("n", "n", "d"),
            ("mean", "mean (J)", ".2f"),
            ("sd", "SD (J)", ".2f"),
            ("median", "median (J)", ".2f"),
            ("min", "min (J)", ".2f"),
            ("max", "max (J)", ".2f"),
        ):
            rows.append([f"{name} {label}", _num(stats_a[field], spec), _num(stats_b[field], spec)])
    runtime_a, runtime_b = version_a["runtime_s"] or {}, version_b["runtime_s"] or {}
    rows.append(
        [
            "Runtime mean ± SD (s)",
            f"{_num(runtime_a.get('mean'), '.1f')} ± {_num(runtime_a.get('sd'), '.1f')}",
            f"{_num(runtime_b.get('mean'), '.1f')} ± {_num(runtime_b.get('sd'), '.1f')}",
        ]
    )
    lines = ["## 2. Descriptive Statistics", ""]
    lines += _table(header, rows)
    lines += _figure_line(figure_relpaths, "fig2", "Energy distribution")
    return lines


def _render_comparison(summary: dict, figure_relpaths: dict[str, str]) -> list[str]:
    comparisons = [("CPU energy", summary["comparison"]["cpu_energy_j"])]
    if summary["comparison"]["dram_energy_j"]:
        comparisons.append(("DRAM energy", summary["comparison"]["dram_energy_j"]))

    def column(comparison: dict | None) -> list[str]:
        if comparison is None:
            return ["n/a"] * 8
        welch = comparison["welch"]
        relative = comparison["relative_difference"]
        adjusted = comparison["order_adjusted"] or {}
        return [
            _num(comparison["difference_j"], "+.2f"),
            _interval(welch["ci95_j"]),
            _num(relative["percent"], "+.2f") + "%",
            _interval(relative["ci95_percent"], unit="%"),
            f"{welch['t']:.2f} ({welch['df']:.1f})",
            _p_value(welch["p"]),
            _p_value(comparison["mann_whitney_p"]),
            f"{_num(adjusted.get('difference_j'), '+.2f')} {_interval(adjusted.get('ci95_j'))}",
        ]

    labels = [
        "Difference `b` - `a` (J)",
        "Welch 95% CI (J)",
        "Relative difference",
        "Fieller 95% CI",
        "Welch t (df)",
        "Welch p",
        "Mann-Whitney U p",
        "Run-order-adjusted difference (J) [95% CI]",
    ]
    columns = [column(comparison) for _, comparison in comparisons]
    rows = [[label, *(values[i] for values in columns)] for i, label in enumerate(labels)]
    lines = ["## 3. Comparison", ""]
    lines += _table(["", *(name for name, _ in comparisons)], rows)
    lines += [
        "Welch's t-test does not assume equal variances. The relative difference is `b` over `a` "
        "minus one, with a Fieller interval. Mann-Whitney U is a robustness check. The run-order "
        "adjustment is a least-squares fit of energy on version and execution slot.",
        "",
    ]
    lines += _figure_line(figure_relpaths, "fig1", "Energy comparison")
    return lines


def _render_containers(summary: dict, figure_relpaths: dict[str, str]) -> list[str]:
    version_a, version_b = summary["versions"]["a"], summary["versions"]["b"]
    breakdown_a = version_a["container_breakdown"]
    breakdown_b = version_b["container_breakdown"]
    lines = ["## 4. Per-Container Energy", ""]
    if not breakdown_a or not breakdown_b:
        return lines + [
            "Not available: no repetition of one of the versions has an archived per-thread "
            "export with its `container_map.json`.",
            "",
        ]
    rows = [
        [
            f"`{entry['role']}`",
            _num(breakdown_a.get(entry["role"]), ".2f"),
            _num(breakdown_b.get(entry["role"]), ".2f"),
            _num(entry["delta_j"], "+.2f"),
            _interval(entry["welch_ci95_j"]),
        ]
        for entry in summary["comparison"]["container_deltas"]
    ]
    lines += _table(
        ["Container", "`a` mean (J)", "`b` mean (J)", "Difference (J)", "Welch 95% CI (J)"],
        rows,
    )
    lines += [
        "Mean CPU energy per repetition per container. The intervals are not adjusted for the "
        "number of containers.",
        "",
    ]
    lines += _figure_line(figure_relpaths, "fig4", "Container breakdown")
    lines += _figure_line(figure_relpaths, "fig5", "Per-container change")
    return lines


_SUPPLEMENTARY_FIGURES = (
    (
        "fig6",
        "Active and idle energy",
        "Active and idle CPU energy per version; idle is METRION's time share of the measured "
        "idle power.",
    ),
    ("fig7", "Thread breakdown", "Thread-level CPU energy inside the highest-energy containers."),
    (
        "fig8",
        "Per-step breakdown",
        "Mean CPU energy measured during each declared step, in scenario order.",
    ),
    (
        "fig9",
        "Per-step change",
        "Change in energy measured during each step; the background worker queue shifts work "
        "between steps, so these are not per-step effects.",
    ),
    (
        "fig10",
        "Per-iteration energy",
        "Mean energy per iteration of the repeated step block, with SD whiskers; work also shifts "
        "between iterations.",
    ),
)


def _render_supplementary(figure_relpaths: dict[str, str]) -> list[str]:
    present = [entry for entry in _SUPPLEMENTARY_FIGURES if entry[0] in figure_relpaths]
    if not present:
        return []
    lines = ["## Supplementary Figures", ""]
    for key, alt, caption in present:
        lines += [caption, "", f"![{alt}]({figure_relpaths[key]})", ""]
    return lines


def render_report(summary: dict, figure_relpaths: dict[str, str]) -> str:
    """The human-readable artifact, rendered from `summary` alone, one helper per section."""
    lines = [
        f"# Evaluation Report: scenario {summary['scenario_id']}, "
        f"comparison_session {summary['comparison_session_id']}",
        "",
        f"Generated {summary['generated_at']}.",
        "",
    ]
    lines += _render_setup(summary, figure_relpaths)
    lines += _render_descriptives(summary, figure_relpaths)
    lines += _render_comparison(summary, figure_relpaths)
    lines += _render_containers(summary, figure_relpaths)
    lines += _render_supplementary(figure_relpaths)
    return "\n".join(lines).rstrip("\n") + "\n"


def _host_slug(host_descriptor: str | None) -> str:
    """Turn `comparison_session.host_descriptor` into one safe path component.

    The descriptor is free text on the session row, so it cannot be used as a directory
    name as it stands: it has held a bare hostname so far, but nothing constrains it to
    and a value with a slash in it would silently write outside `output_root`.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (host_descriptor or "").strip()).strip("-.")
    return slug or "unknown-host"


def run_evaluation(
    session: Session, comparison_session_id: int, output_root: Path = Path("evaluation")
) -> Path:
    """Compute the summary, render the figures/report, and write them under `output_root`."""
    comparison_session = session.get(ComparisonSession, comparison_session_id)
    if comparison_session is None:
        raise EvaluationError(f"no comparison_session with id={comparison_session_id}")

    version_runs = comparison_session.version_runs
    if len(version_runs) != 2:
        raise EvaluationError(
            f"comparison_session {comparison_session_id} has {len(version_runs)} "
            "version_runs, expected 2"
        )

    equivalence_check = _find_equivalence_check(session, comparison_session)
    if equivalence_check is None:
        raise EvaluationError(
            f"no equivalence_check found for comparison_session {comparison_session_id}"
        )

    runs_by_version_id = {vr.app_version_id: vr for vr in version_runs}
    version_a_run = runs_by_version_id[equivalence_check.version_a_id]
    version_b_run = runs_by_version_id[equivalence_check.version_b_id]
    prepared_a = _prepare_version(version_a_run.app_version, version_a_run)
    prepared_b = _prepare_version(version_b_run.app_version, version_b_run)
    # A version run with no usable energy readings has nothing to compare, and every statistic
    # downstream divides by that count: without this, an abandoned or all-failed session surfaced
    # as a ZeroDivisionError from inside the effect-size helper rather than as a clear refusal.
    for prepared in (prepared_a, prepared_b):
        if not prepared.cpu_values:
            raise EvaluationError(
                f"app_version {prepared.app_version_id} ({prepared.label}) has no usable "
                f"repetitions in comparison_session {comparison_session_id}, nothing to compare"
            )

    summary = build_summary(comparison_session, equivalence_check, prepared_a, prepared_b)

    # Host-qualified, because `<scenario>_<session>` alone collides across machines: session ids
    # restart at 1 on every database, so two different hosts' session 3 would both ask for
    # `evaluation/1_3` and the second generated would overwrite the first in place with nothing
    # said. The host is the axis that disambiguates them because it is the axis a second
    # database implies.
    output_dir = output_root / _host_slug(comparison_session.host_descriptor)
    output_dir = output_dir / f"{comparison_session.scenario_id}_{comparison_session_id}"
    # Refuse rather than overwrite, and refuse on any existing content rather than only on a
    # report: regenerating is cheap only while the session's row and its archives still exist,
    # so silently replacing the previous contents is the one outcome with no way back. Deleting
    # the directory is the deliberate act that allows it, and there is no --force, because a
    # flag that means "overwrite the thing I might want" is the same trap one keystroke further
    # away.
    if output_dir.exists() and any(output_dir.iterdir()):
        raise EvaluationError(
            f"{output_dir} already exists and is not empty; refusing to overwrite it. "
            "Delete it first if the report is meant to be regenerated."
        )
    output_dir.mkdir(parents=True, exist_ok=True)

    figure_a, figure_b = _figure_versions(prepared_a, prepared_b)
    _figure_bar_chart(figure_a, figure_b, output_dir / "fig1_energy_comparison.png")
    _figure_distribution(figure_a, figure_b, output_dir / "fig2_energy_distribution.png")
    _figure_run_order_trend(
        comparison_session.execution_order,
        equivalence_check.version_a_id,
        figure_a,
        equivalence_check.version_b_id,
        figure_b,
        output_dir / "fig3_run_order_trend.png",
    )

    figure_relpaths = {
        "fig1": "fig1_energy_comparison.png",
        "fig2": "fig2_energy_distribution.png",
        "fig3": "fig3_run_order_trend.png",
    }
    breakdown_a = summary["versions"]["a"]["container_breakdown"]
    breakdown_b = summary["versions"]["b"]["container_breakdown"]
    if breakdown_a and breakdown_b:
        _figure_container_breakdown(figure_a, figure_b, output_dir / "fig4_container_breakdown.png")
        figure_relpaths["fig4"] = "fig4_container_breakdown.png"

        _figure_role_deltas(figure_a, figure_b, output_dir / "fig5_container_deltas.png")
        figure_relpaths["fig5"] = "fig5_container_deltas.png"

        # Targets whichever containers actually carry the most energy, rather than named ones.
        roles = _top_roles(breakdown_a, breakdown_b, limit=THREAD_BREAKDOWN_CONTAINERS)
        if roles:
            _figure_thread_breakdown(
                figure_a, figure_b, roles, output_dir / "fig7_thread_breakdown.png"
            )
            figure_relpaths["fig7"] = "fig7_thread_breakdown.png"
            summary["thread_breakdown_containers"] = roles

    if summary["step_order"]:
        _figure_step_breakdown(
            figure_a, figure_b, summary["step_order"], output_dir / "fig8_step_breakdown.png"
        )
        figure_relpaths["fig8"] = "fig8_step_breakdown.png"
        _figure_step_deltas(
            figure_a, figure_b, summary["step_order"], output_dir / "fig9_step_deltas.png"
        )
        figure_relpaths["fig9"] = "fig9_step_deltas.png"

    if summary.get("per_iteration"):
        _figure_iteration_trend(figure_a, figure_b, output_dir / "fig10_iteration_trend.png")
        figure_relpaths["fig10"] = "fig10_iteration_trend.png"

    if summary["versions"]["a"]["energy_split"] and summary["versions"]["b"]["energy_split"]:
        _figure_energy_split(figure_a, figure_b, output_dir / "fig6_energy_split.png")
        figure_relpaths["fig6"] = "fig6_energy_split.png"

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    report_md = render_report(summary, figure_relpaths)
    (output_dir / "comparison_report.md").write_text(report_md)
    return output_dir
