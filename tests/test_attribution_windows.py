"""The exact interval-overlap weighting in `metrilens.attribution`.

The evaluation module builds the per-step decomposition from it, so these tests pin the
arithmetic itself.
"""

import csv
from datetime import datetime, timedelta

import pytest

from metrilens.attribution import (
    overlap_weighted_energy_j,
    read_role_intervals,
    step_energy_j,
)

BASE = datetime(2026, 8, 13, 12, 0, 0)


def _intervals(count: int, energy_per_interval: float = 10.0):
    """`count` contiguous one-second intervals, matching METRION's own sampling grid."""
    return [
        (BASE + timedelta(seconds=i), BASE + timedelta(seconds=i + 1), energy_per_interval)
        for i in range(count)
    ]


class TestOverlapWeighting:
    def test_a_span_covering_whole_intervals_takes_all_of_them(self):
        assert overlap_weighted_energy_j(
            _intervals(3), BASE, BASE + timedelta(seconds=3)
        ) == pytest.approx(30.0)

    def test_a_span_inside_one_interval_takes_its_share(self):
        """The case the whole thing exists for: a step's span is sub-second on both ends, and
        `analyze` cannot express it, since it selects whole intervals at second resolution."""
        assert overlap_weighted_energy_j(
            _intervals(1), BASE + timedelta(milliseconds=250), BASE + timedelta(milliseconds=750)
        ) == pytest.approx(5.0)

    def test_a_span_straddling_two_intervals_takes_a_share_of_each(self):
        energy = overlap_weighted_energy_j(
            _intervals(2), BASE + timedelta(milliseconds=800), BASE + timedelta(milliseconds=1400)
        )
        assert energy == pytest.approx(10.0 * 0.2 + 10.0 * 0.4)

    def test_intervals_outside_the_span_contribute_nothing(self):
        assert overlap_weighted_energy_j(
            _intervals(5), BASE + timedelta(seconds=2), BASE + timedelta(seconds=3)
        ) == pytest.approx(10.0)

    def test_a_span_touching_an_interval_boundary_takes_nothing_from_it(self):
        """Zero-length overlap is not a contribution, otherwise a step ending exactly on a
        boundary would draw energy from the second after it."""
        assert overlap_weighted_energy_j(
            [(BASE, BASE + timedelta(seconds=1), 10.0)],
            BASE + timedelta(seconds=1),
            BASE + timedelta(seconds=2),
        ) == pytest.approx(0.0)

    def test_the_parts_sum_to_the_whole(self):
        """Contiguous spans partition the energy exactly, which is what lets the report present a
        per-step decomposition as a decomposition rather than as unrelated samples."""
        intervals = _intervals(4)
        cuts = [0, 1.3, 2.75, 4.0]
        parts = [
            overlap_weighted_energy_j(
                intervals, BASE + timedelta(seconds=lo), BASE + timedelta(seconds=hi)
            )
            for lo, hi in zip(cuts, cuts[1:], strict=False)
        ]
        assert sum(parts) == pytest.approx(40.0)


class TestStepEnergy:
    def test_it_sums_a_step_across_its_iterations(self):
        """A k=3 scenario logs `browse_catalog` three times, and fig8 plots one segment for it."""
        spans = [
            ("browse_catalog", BASE, BASE + timedelta(seconds=1)),
            ("create_part", BASE + timedelta(seconds=1), BASE + timedelta(seconds=2)),
            ("browse_catalog", BASE + timedelta(seconds=2), BASE + timedelta(seconds=3)),
        ]
        totals = step_energy_j(_intervals(3), spans)
        assert totals == {"browse_catalog": pytest.approx(20.0), "create_part": pytest.approx(10.0)}

    def test_a_step_that_overlaps_nothing_still_appears_as_zero(self):
        """Dropping it would make a step silently vanish from the decomposition, where a zero is
        a visible statement that the step spent nothing in the measured window."""
        spans = [("orphan", BASE + timedelta(hours=5), BASE + timedelta(hours=5, seconds=1))]
        assert step_energy_j(_intervals(3), spans) == {"orphan": pytest.approx(0.0)}


class TestReadRoleIntervals:
    def _export(self, tmp_path, rows):
        path = tmp_path / "raw_cpu.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "interval_start",
                    "interval_end",
                    "total_energy_j",
                    "resource_group_name",
                    "resource_group_id",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_it_keeps_the_target_stack_and_drops_everything_else(self, tmp_path):
        """The export covers the whole host, so an unfiltered read would report
        other accounts' processes as the workload's."""
        path = self._export(
            tmp_path,
            [
                {
                    "interval_start": BASE.isoformat(),
                    "interval_end": (BASE + timedelta(seconds=1)).isoformat(),
                    "total_energy_j": "4.0",
                    "resource_group_name": "/docker-aaaaaaaaaaaa.scope",
                    "resource_group_id": "1",
                },
                {
                    "interval_start": BASE.isoformat(),
                    "interval_end": (BASE + timedelta(seconds=1)).isoformat(),
                    "total_energy_j": "99.0",
                    "resource_group_name": "/user.slice/someone-else",
                    "resource_group_id": "2",
                },
            ],
        )

        intervals = read_role_intervals(path, {"aaaaaaaaaaaa": "inventree-server"})

        assert [energy for _, _, energy in intervals] == [4.0]
        assert overlap_weighted_energy_j(
            intervals, BASE, BASE + timedelta(seconds=1)
        ) == pytest.approx(4.0)
