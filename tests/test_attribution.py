"""Container selection and active/idle split over METRION's per-thread raw CPU export."""

import pytest
from conftest import CONTAINER_MAP, SERVER_CGROUP, write_detailed_export, write_raw_export

from metrilens.attribution import container_energy_j, container_energy_split


class TestParseContainerEnergy:
    def test_sums_by_container_role(self, tmp_path):
        raw_path = tmp_path / "raw_cpu_x.csv"
        write_raw_export(
            raw_path,
            [
                ("/system.slice/docker-abc123def456xx.scope", 5.0),
                ("/system.slice/docker-abc123def456xx.scope", 3.0),
                ("/system.slice/docker-fedcba987654xx.scope", 2.0),
                ("", 1.0),
            ],
        )
        container_map = {"abc123def456": "inventree-server", "fedcba987654": "inventree-db"}

        totals = container_energy_j(raw_path, container_map)

        assert totals == {"inventree-server": 8.0, "inventree-db": 2.0}

    def test_rows_outside_the_target_stack_are_excluded(self, tmp_path):
        """The export covers the whole host, so rows outside the stack hold every other tenant's
        work and have no place in the workload's own total."""
        raw_path = tmp_path / "raw_cpu_x.csv"
        write_raw_export(raw_path, [("/system.slice/docker-000000000000xx.scope", 4.0)])

        assert container_energy_j(raw_path, {}) == {}

    def test_empty_group_name_resolved_via_shared_group_id(self, tmp_path):
        """METRION leaves `resource_group_name` empty on a subset of thread rows while still
        populating `resource_group_id` with the same id its resolvable rows carry; those rows
        belong to that container, not to "other". Misfiling them can move a large share of the
        measured energy."""
        raw_path = tmp_path / "raw_cpu_x.csv"
        write_raw_export(
            raw_path,
            [
                ("/system.slice/docker-abc123def456xx.scope", "9028467", 5.0),
                ("", "9028467", 3.0),
                ("", "", 1.0),
            ],
        )

        totals = container_energy_j(raw_path, {"abc123def456": "inventree-server"})

        assert totals == {"inventree-server": 8.0}


class TestEnergySplit:
    def test_sums_active_and_idle_and_reconciles(self, tmp_path):
        path = tmp_path / "raw_cpu_x.csv"
        write_detailed_export(
            path,
            [
                {
                    "resource_group_name": SERVER_CGROUP,
                    "total_energy_j": 10.0,
                    "active_energy_j": 8.0,
                    "idle_energy_j": 2.0,
                },
                {
                    "resource_group_name": SERVER_CGROUP,
                    "total_energy_j": 5.0,
                    "active_energy_j": 3.0,
                    "idle_energy_j": 2.0,
                },
            ],
        )
        split = container_energy_split(path, CONTAINER_MAP)
        assert split.active_j == pytest.approx(11.0)
        assert split.idle_j == pytest.approx(4.0)
        assert split.total_j == pytest.approx(15.0)
        assert split.reconciles

    def test_detects_non_reconciling_export(self, tmp_path):
        """`E_active + E_idle == E_total` holds by construction (METRION paper equation 2), so a
        violation means the export or this parsing is broken, not that the machine misbehaved."""
        path = tmp_path / "raw_cpu_x.csv"
        write_detailed_export(
            path,
            [
                {
                    "resource_group_name": SERVER_CGROUP,
                    "total_energy_j": 10.0,
                    "active_energy_j": 8.0,
                    "idle_energy_j": 5.0,
                }
            ],
        )
        assert not container_energy_split(path, CONTAINER_MAP).reconciles

    def test_excludes_rows_outside_the_target_stack(self, tmp_path):
        """The export covers the whole host, so a split summed over every row
        would describe the machine rather than the workload."""
        path = tmp_path / "raw_cpu_x.csv"
        write_detailed_export(
            path,
            [
                {
                    "resource_group_name": SERVER_CGROUP,
                    "total_energy_j": 10.0,
                    "active_energy_j": 8.0,
                    "idle_energy_j": 2.0,
                },
                {
                    "resource_group_name": "/system.slice/docker-999999999999xx.scope",
                    "total_energy_j": 900.0,
                    "active_energy_j": 800.0,
                    "idle_energy_j": 100.0,
                },
                {"total_energy_j": 40.0, "active_energy_j": 30.0, "idle_energy_j": 10.0},
            ],
        )
        split = container_energy_split(path, CONTAINER_MAP)
        assert split.total_j == pytest.approx(10.0)
        assert split.reconciles
