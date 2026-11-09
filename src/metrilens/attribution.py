"""Attributes METRION's per-thread export rows to the target app's containers.

`metrion analyze` is asked for an unfiltered window, because a PID list taken before the
monitor starts would miss processes the stack forks later. Rows are selected here by cgroup
instead, which also catches short-lived processes such as per-connection database backends.
The writer and the evaluation module both rely on this selection, so it lives in one place.

Everything here is arithmetic over METRION's exported rows: summing the raw export over a
window reproduces `analyze`'s own total.
"""

import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The bucket for rows neither resolution stage can attribute to a container, and for every
# process on the host outside the target stack (the export is unfiltered, so this is not
# expected to be small). Kept rather than dropped, so any aggregation reconciles against the
# export it came from.
OTHER_ROLE = "other"

_CONTAINER_ID_PATTERN = re.compile(r"docker-([0-9a-f]{12})")


@dataclass(frozen=True)
class EnergySplit:
    """METRION's own active/idle decomposition of one selection of rows."""

    active_j: float
    idle_j: float
    total_j: float

    @property
    def reconciles(self) -> bool:
        """Whether `active_j + idle_j == total_j`, which holds by construction, so a
        violation means the export or this parsing is wrong, not that the machine behaved
        oddly. Checked rather than assumed."""
        return abs(self.active_j + self.idle_j - self.total_j) <= max(1e-6, 1e-9 * self.total_j)


def read_export_rows(raw_export_path: Path) -> list[dict]:
    """Read METRION's raw per-thread CSV export into a list of dict rows."""
    with raw_export_path.open(newline="") as handle:
        return list(csv.DictReader(handle))


# How many seconds of the measured window an export may lack before it counts as incomplete. A
# healthy export is a second or two short, because `analyze` drops a boundary interval it cannot
# fully contain; a monitor killed during startup leaves far more missing.
MAX_MISSING_INTERVALS = 3


def interval_coverage(raw_export_path: Path) -> int:
    """How many distinct one-second sampling intervals the export actually holds.

    The export is per thread per interval, so the distinct `interval_start` values are the
    seconds METRION returned, independently of how many threads ran in each. Callers compare
    this against the measured window (the whole seconds `analyze` was asked for), not the
    unrounded step span.
    """
    starts = {row["interval_start"] for row in read_export_rows(raw_export_path)}
    return len(starts)


def role_by_group_name(row: dict, container_map: dict[str, str]) -> str | None:
    """Container role from `resource_group_name`'s embedded container ID, `None` if the row
    carries no cgroup path or names a container outside this Compose project."""
    match = _CONTAINER_ID_PATTERN.search(row.get("resource_group_name") or "")
    return container_map.get(match.group(1)) if match else None


def roles_by_group_id(rows: list[dict], container_map: dict[str, str]) -> dict[str, str]:
    """`resource_group_id` -> container role, learned from whichever rows in this same export
    do carry a resolvable `resource_group_name`.

    METRION leaves `resource_group_name` empty on a subset of thread rows while still
    populating `resource_group_id`, and that id is shared with rows that do carry the cgroup
    path. Without this second resolution stage those rows fall into `OTHER_ROLE`, which
    concentrates on one container's worker threads rather than spreading evenly, so skipping
    this stage can silently misattribute a large share of a container's real energy.
    """
    learned: dict[str, str] = {}
    for row in rows:
        role = role_by_group_name(row, container_map)
        group_id = row.get("resource_group_id") or ""
        if role and group_id:
            learned[group_id] = role
    return learned


def rows_with_roles(rows: list[dict], container_map: dict[str, str]) -> list[tuple[dict, str]]:
    """Each row paired with its container role, resolved from `resource_group_name` first and
    then from `roles_by_group_id`'s learned mapping. Rows neither stage can attribute are
    bucketed under `OTHER_ROLE` rather than dropped, so any aggregation over this reconciles
    against the export it was read from."""
    learned = roles_by_group_id(rows, container_map)
    return [
        (
            row,
            role_by_group_name(row, container_map)
            or learned.get(row.get("resource_group_id") or "")
            or OTHER_ROLE,
        )
        for row in rows
    ]


def container_energy_j(raw_export_path: Path, container_map: dict[str, str]) -> dict[str, float]:
    """Sum `total_energy_j` per container role, excluding `OTHER_ROLE`.

    Excluded rather than returned alongside, since the export covers the whole host and `other`
    is dominated by processes that have nothing to do with the measurement. A caller that wants
    the residual can sum `rows_with_roles` itself.
    """
    totals: dict[str, float] = {}
    for row, role in rows_with_roles(read_export_rows(raw_export_path), container_map):
        if role == OTHER_ROLE:
            continue
        totals[role] = totals.get(role, 0.0) + float(row["total_energy_j"])
    return totals


def container_energy_split(raw_export_path: Path, container_map: dict[str, str]) -> EnergySplit:
    """METRION's active and idle columns summed over the target stack's containers only.

    Scoped the same way `container_energy_j` is, so that `active_j + idle_j == total_j` still
    holds against the repetition's own `cpu_energy_j` rather than against the host.
    """
    active = idle = total = 0.0
    for row, role in rows_with_roles(read_export_rows(raw_export_path), container_map):
        if role == OTHER_ROLE:
            continue
        active += float(row["active_energy_j"])
        idle += float(row["idle_energy_j"])
        total += float(row["total_energy_j"])
    return EnergySplit(active, idle, total)


def read_role_intervals(
    raw_export_path: Path, container_map: dict[str, str]
) -> list[tuple[datetime, datetime, float]]:
    """`(interval_start, interval_end, energy_j)` for the target stack's rows only.

    The same row selection `container_energy_j` uses, kept per interval rather than summed, which
    is what any question about *when* inside the window the energy was spent needs.
    """
    intervals = []
    for row, role in rows_with_roles(read_export_rows(raw_export_path), container_map):
        if role == OTHER_ROLE:
            continue
        intervals.append(
            (
                datetime.fromisoformat(row["interval_start"]),
                datetime.fromisoformat(row["interval_end"]),
                float(row["total_energy_j"]),
            )
        )
    return intervals


def overlap_weighted_energy_j(
    intervals: list[tuple[datetime, datetime, float]], start: datetime, end: datetime
) -> float:
    """Energy over an exact span, each interval weighted by how much of it lies inside.

    METRION samples in contiguous one-second intervals and `analyze` selects whole ones, so no
    request can express a sub-second span; a step's span is sub-second on both ends. Weighting by
    overlap lets a step shorter than the grid be attributed at all, and is why the per-step
    numbers sum to slightly less than the measured window, which is rounded outward to whole
    seconds.
    """
    total = 0.0
    for interval_start, interval_end, energy in intervals:
        overlap = (min(interval_end, end) - max(interval_start, start)).total_seconds()
        if overlap > 0:
            total += energy * overlap / (interval_end - interval_start).total_seconds()
    return total


def step_energy_j(
    intervals: list[tuple[datetime, datetime, float]],
    spans: list[tuple[str, datetime, datetime]],
) -> dict[str, float]:
    """`step_id -> energy`, summing a step's occurrences across iterations.

    Aggregating by step id here rather than keeping each occurrence separate is deliberate: it's
    what the per-step figures plot, with the per-iteration view built separately so each figure
    answers one question. A caller wanting per iteration calls `overlap_weighted_energy_j` per
    span.
    """
    totals: dict[str, float] = {}
    for step_id, start, end in spans:
        totals[step_id] = totals.get(step_id, 0.0) + overlap_weighted_energy_j(
            intervals, start, end
        )
    return totals
