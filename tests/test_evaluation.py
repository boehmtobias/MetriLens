import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import (
    write_detailed_export as _write_detailed_export,
)
from conftest import (
    write_raw_export as _write_raw_export,
)

from metrilens.evaluation import (
    TICK_LABEL_WRAP_WIDTH,
    EvaluationError,
    _compare,
    _describe,
    _energy_panels,
    _execution_slots,
    _fieller_ci_percent,
    _figure_bar_chart,
    _figure_distribution,
    _figure_iteration_trend,
    _figure_role_deltas,
    _figure_run_order_trend,
    _figure_step_breakdown,
    _figure_step_deltas,
    _figure_versions,
    _host_slug,
    _order_adjusted,
    _ordered_steps,
    _parse_thread_energy,
    _PreparedVersion,
    _repetition_container_energy,
    _shared_label_prefix,
    _significant_with_other,
    _t_ci_mean,
    _thread_name,
    _tick_label,
    _top_roles,
    _version_container_breakdown,
    _welch_ci_difference,
    build_summary,
    render_report,
    run_evaluation,
)
from metrilens.models import (
    ComparisonSession,
    EquivalenceCheck,
    EquivalenceVerdict,
    Repetition,
    RepetitionStatus,
    Scenario,
    VersionRun,
)


def _prepared(
    app_version_id: int,
    label: str,
    cpu_values: list[float],
    dram_values: list[float] | None = None,
    runtime_values: list[float] | None = None,
    noted_repetition_ids: tuple[int, ...] = (),
) -> _PreparedVersion:
    """One version with one usable repetition per CPU value, repetition indices 0, 1, ..."""
    dram_values = dram_values or []
    runtime_values = runtime_values or [50.0] * len(cpu_values)
    ok_repetitions = [
        SimpleNamespace(
            id=index + 1,
            repetition_index=index,
            status=RepetitionStatus.OK,
            cpu_energy_j=cpu,
            dram_energy_j=dram_values[index] if index < len(dram_values) else None,
            runtime_s=runtime_values[index],
            raw_export_ref=None,
            step_log_ref=None,
            measurement_started_at=None,
            start_time=datetime(2026, 1, 1, 12, 0, index, tzinfo=UTC),
            end_time=datetime(2026, 1, 1, 12, 1, index, tzinfo=UTC),
            attempt_count=1 if index + 1 in noted_repetition_ids else 0,
            validation_notes=(
                "attempt 1 failed: metrion monitor stop failure"
                if index + 1 in noted_repetition_ids
                else None
            ),
        )
        for index, cpu in enumerate(cpu_values)
    ]
    return _PreparedVersion(
        app_version_id=app_version_id,
        label=label,
        ok_repetitions=ok_repetitions,
        cpu_values=cpu_values,
        dram_values=dram_values,
        runtime_values=runtime_values,
    )


class TestExecutionSlots:
    def test_reconstructs_per_version_repetition_index(self):
        slots = _execution_slots([1, 2, 1, 1, 2])
        assert slots == [(1, 0), (2, 0), (1, 1), (1, 2), (2, 1)]


class TestEnergyPanels:
    """Every energy figure derives its panels from this, so DRAM appears in all of them or
    none."""

    def test_cpu_only_when_no_dram_measured(self):
        panels = _energy_panels(_prepared(1, "A", [1.0, 2.0]), _prepared(2, "B", [1.0, 2.0]))
        assert [component for component, _, _ in panels] == ["CPU energy"]

    def test_adds_dram_when_both_versions_measured_it(self):
        panels = _energy_panels(
            _prepared(1, "A", [1.0, 2.0], dram_values=[0.5, 0.6]),
            _prepared(2, "B", [1.0, 2.0], dram_values=[0.5, 0.6]),
        )
        assert [component for component, _, _ in panels] == ["CPU energy", "DRAM energy"]

    def test_omits_dram_when_only_one_version_measured_it(self):
        panels = _energy_panels(
            _prepared(1, "A", [1.0, 2.0], dram_values=[0.5, 0.6]),
            _prepared(2, "B", [1.0, 2.0]),
        )
        assert [component for component, _, _ in panels] == ["CPU energy"]


class TestDescribe:
    def test_reports_the_six_descriptives(self):
        assert _describe([1.0, 2.0, 3.0, 10.0]) == {
            "n": 4,
            "mean": 4.0,
            "sd": pytest.approx(np.std([1.0, 2.0, 3.0, 10.0], ddof=1)),
            "median": 2.5,
            "min": 1.0,
            "max": 10.0,
        }

    def test_one_value_has_no_sd_and_none_has_nothing(self):
        assert _describe([5.0])["sd"] is None
        assert _describe([])["mean"] is None


_BASELINE = [100.0, 102.0, 98.0, 101.0, 99.5, 100.5, 103.0, 97.0]
_OTHER = [110.0, 108.0, 113.0, 109.5, 111.0, 107.0, 112.5, 110.5]


class TestCompare:
    def test_welch_and_mann_whitney_match_scipy(self):
        from scipy import stats

        result = _compare(_BASELINE, _OTHER)
        welch = stats.ttest_ind(_OTHER, _BASELINE, equal_var=False)
        assert result["difference_j"] == pytest.approx(np.mean(_OTHER) - np.mean(_BASELINE))
        assert result["welch"]["t"] == pytest.approx(welch.statistic)
        assert result["welch"]["df"] == pytest.approx(welch.df)
        assert result["welch"]["p"] == pytest.approx(welch.pvalue)
        interval = welch.confidence_interval(0.95)
        assert result["welch"]["ci95_j"] == pytest.approx([interval.low, interval.high])
        assert result["mann_whitney_p"] == pytest.approx(
            stats.mannwhitneyu(_OTHER, _BASELINE, alternative="two-sided").pvalue
        )

    def test_relative_difference_is_b_over_a(self):
        relative = _compare(_BASELINE, _OTHER)["relative_difference"]
        expected = (np.mean(_OTHER) / np.mean(_BASELINE) - 1) * 100
        assert relative["percent"] == pytest.approx(expected)
        low, high = relative["ci95_percent"]
        assert low < expected < high
        assert relative["method"] == "fieller"

    def test_too_few_values_return_none(self):
        assert _compare([1.0], [2.0, 3.0]) is None


class TestFiellerInterval:
    def test_endpoints_solve_the_fieller_equation(self):
        """Each endpoint r satisfies (mean_b - r mean_a)^2 = t^2 (var_b + r^2 var_a)."""
        from scipy import stats

        a, b = np.asarray(_BASELINE), np.asarray(_OTHER)
        var_a, var_b = np.var(a, ddof=1) / len(a), np.var(b, ddof=1) / len(b)
        df = (var_a + var_b) ** 2 / (var_a**2 / (len(a) - 1) + var_b**2 / (len(b) - 1))
        t = stats.t.ppf(0.975, df)
        for percent in _fieller_ci_percent(_BASELINE, _OTHER):
            r = percent / 100 + 1
            lhs = (b.mean() - r * a.mean()) ** 2
            assert lhs == pytest.approx(t**2 * (var_b + r**2 * var_a))

    def test_close_to_the_delta_method_for_a_precise_baseline(self):
        a, b = np.asarray(_BASELINE), np.asarray(_OTHER)
        ratio = b.mean() / a.mean()
        se = ratio * np.sqrt(
            np.var(a, ddof=1) / len(a) / a.mean() ** 2 + np.var(b, ddof=1) / len(b) / b.mean() ** 2
        )
        low, high = _fieller_ci_percent(_BASELINE, _OTHER)
        assert low == pytest.approx((ratio - 2.2 * se - 1) * 100, abs=1.0)
        assert high == pytest.approx((ratio + 2.2 * se - 1) * 100, abs=1.0)

    def test_unbounded_when_the_baseline_mean_is_not_clearly_nonzero(self):
        assert _fieller_ci_percent([-1.0, 1.0, -2.0, 2.0], [5.0, 6.0, 7.0, 8.0]) is None


class TestOrderAdjusted:
    def test_matches_ordinary_least_squares(self):
        slots_a, slots_b = list(range(0, 16, 2)), list(range(1, 16, 2))
        result = _order_adjusted(_BASELINE, slots_a, _OTHER, slots_b)
        y = np.asarray(_BASELINE + _OTHER)
        design = np.column_stack([np.ones(16), [0] * 8 + [1] * 8, slots_a + slots_b])
        coefficients = np.linalg.lstsq(design, y, rcond=None)[0]
        assert result["difference_j"] == pytest.approx(coefficients[1])
        low, high = result["ci95_j"]
        assert low < coefficients[1] < high

    def test_removes_a_drift_that_loads_onto_one_version(self):
        """A pure 1 J/slot drift with no version effect, where b runs later, reads as a
        difference unadjusted and as none adjusted."""
        slots_a, slots_b = list(range(8)), list(range(8, 16))
        baseline = [100.0 + slot + noise for slot, noise in zip(slots_a, [0.1, -0.1] * 4)]
        other = [100.0 + slot + noise for slot, noise in zip(slots_b, [-0.1, 0.1] * 4)]
        assert _compare(baseline, other)["difference_j"] == pytest.approx(8.0, abs=0.2)
        adjusted = _order_adjusted(baseline, slots_a, other, slots_b)
        assert adjusted["difference_j"] == pytest.approx(0.0, abs=0.2)


class TestFigureIntervals:
    def test_t_ci_mean_matches_scipy(self):
        from scipy import stats

        values = [10.0, 11.5, 9.8, 12.1, 10.7]
        expected = stats.t.interval(
            0.95, len(values) - 1, loc=np.mean(values), scale=stats.sem(values)
        )
        assert _t_ci_mean(values) == pytest.approx(list(expected))

    def test_t_ci_mean_degenerates_to_the_value_for_one_sample(self):
        assert _t_ci_mean([4.0]) == [4.0, 4.0]

    def test_welch_ci_difference_is_other_minus_baseline(self):
        from scipy import stats

        baseline, other = [10.0, 11.0, 12.0, 10.5], [13.0, 15.5, 14.0, 12.0]
        expected = stats.ttest_ind(other, baseline, equal_var=False).confidence_interval(0.95)
        low, high = _welch_ci_difference(baseline, other)
        assert (low, high) == pytest.approx((expected.low, expected.high))
        assert low > 0

    def test_welch_ci_difference_degenerates_to_the_difference_for_tiny_samples(self):
        assert _welch_ci_difference([1.0], [3.0]) == [2.0, 2.0]


class TestSharedLabelPrefix:
    def test_splits_off_the_leading_words_both_labels_share(self):
        assert _shared_label_prefix("App 1.0 DEBUG=False cache=True", "App 1.0 cache=False") == (
            "App 1.0",
            "DEBUG=False cache=True",
            "cache=False",
        )

    def test_keeps_at_least_one_word_in_each_remainder(self):
        assert _shared_label_prefix("App 1.0", "App 1.0 debug") == ("App", "1.0", "1.0 debug")

    def test_returns_labels_whole_without_a_shared_word(self):
        assert _shared_label_prefix("Baseline", "Modified") == ("", "Baseline", "Modified")

    def test_matches_whole_words_only(self):
        assert _shared_label_prefix("cache=True", "cache=False") == (
            "",
            "cache=True",
            "cache=False",
        )

    def test_figure_versions_change_only_the_label_fields(self):
        prepared_a = _prepared(1, "App 1.0 fast", [1.0, 2.0])
        prepared_b = _prepared(2, "App 1.0 slow", [3.0, 4.0])
        figure_a, figure_b = _figure_versions(prepared_a, prepared_b)
        assert (figure_a.label, figure_b.label) == ("fast", "slow")
        assert figure_a.label_context == figure_b.label_context == "App 1.0"
        assert figure_a.cpu_values == prepared_a.cpu_values
        assert prepared_a.label == "App 1.0 fast"


class TestFigures:
    """Smoke coverage for the figure functions themselves, especially the two-panel DRAM path,
    which hosts without a DRAM RAPL domain cannot exercise."""

    @staticmethod
    def _pair(with_dram: bool):
        cpu_a = [10.0, 11.0, 12.0, 10.5]
        cpu_b = [12.0, 13.0, 14.0, 12.5]
        dram_a = [1.0, 1.1, 1.2, 1.05] if with_dram else None
        dram_b = [1.2, 1.3, 1.4, 1.25] if with_dram else None
        prepared_a = _prepared(1, "Version A with a deliberately long label", cpu_a, dram_a)
        prepared_b = _prepared(2, "Version B with a deliberately long label", cpu_b, dram_b)
        for prepared, cpu, dram in ((prepared_a, cpu_a, dram_a), (prepared_b, cpu_b, dram_b)):
            prepared.ok_repetitions = [
                SimpleNamespace(
                    repetition_index=index,
                    cpu_energy_j=cpu[index],
                    dram_energy_j=dram[index] if dram else None,
                    raw_export_ref=None,
                )
                for index in range(len(cpu))
            ]
        return prepared_a, prepared_b

    @pytest.mark.parametrize("with_dram", [False, True])
    def test_bar_chart_and_distribution_render(self, tmp_path, with_dram):
        prepared_a, prepared_b = self._pair(with_dram)
        for name, func in (
            ("bar", _figure_bar_chart),
            ("dist", _figure_distribution),
        ):
            path = tmp_path / f"{name}_{with_dram}.png"
            func(prepared_a, prepared_b, path)
            assert path.stat().st_size > 0
            # Every figure function funnels through `_save`, so a PDF sibling is written
            # alongside the PNG for every one of them, not just this pair.
            assert path.with_suffix(".pdf").stat().st_size > 0

    @pytest.mark.parametrize("with_dram", [False, True])
    def test_run_order_trend_renders_with_a_fit_per_panel(self, tmp_path, with_dram):
        prepared_a, prepared_b = self._pair(with_dram)
        path = tmp_path / f"trend_{with_dram}.png"
        _figure_run_order_trend([1, 2, 1, 2, 1, 2, 1, 2], 1, prepared_a, 2, prepared_b, path)
        assert path.stat().st_size > 0

    def test_shared_label_context_renders(self, tmp_path):
        prepared_a, prepared_b = _figure_versions(*self._pair(False))
        assert prepared_a.label_context == "Version"
        path = tmp_path / "bar_context.png"
        _figure_bar_chart(prepared_a, prepared_b, path)
        assert path.stat().st_size > 0

    def test_decomposition_figures_render(self, tmp_path, monkeypatch):
        import metrilens.evaluation as evaluation

        prepared_a, prepared_b = self._pair(False)
        steps = ["login", "browse"]
        breakdowns = {1: {"login": 4.0, "browse": 6.5}, 2: {"login": 5.0, "browse": 7.5}}
        iterations = {
            1: {1: [3.0, 3.5, 4.0], 2: [5.0, 5.5, 6.0]},
            2: {1: [3.5, 4.0, 4.5], 2: [6.0, 6.5, 7.0]},
        }
        monkeypatch.setattr(
            evaluation,
            "_version_step_breakdown",
            lambda prepared: breakdowns[prepared.app_version_id],
        )
        monkeypatch.setattr(
            evaluation,
            "_step_deltas",
            lambda a, b, ordered: [(step, 1.0) for step in ordered],
        )
        monkeypatch.setattr(
            evaluation,
            "_version_iteration_series",
            lambda prepared: iterations[prepared.app_version_id],
        )
        monkeypatch.setattr(
            evaluation,
            "_role_deltas",
            lambda a, b: [("db", -0.5, [-1.0, 0.0]), ("server", 2.0, [1.0, 3.0])],
        )
        for name, draw in (
            ("steps", lambda path: _figure_step_breakdown(prepared_a, prepared_b, steps, path)),
            ("deltas", lambda path: _figure_step_deltas(prepared_a, prepared_b, steps, path)),
            ("iterations", lambda path: _figure_iteration_trend(prepared_a, prepared_b, path)),
            ("roles", lambda path: _figure_role_deltas(prepared_a, prepared_b, path)),
        ):
            path = tmp_path / f"{name}.png"
            draw(path)
            assert path.stat().st_size > 0, name

    def test_run_order_trend_tolerates_a_version_with_no_usable_points(self, tmp_path):
        prepared_a, prepared_b = self._pair(False)
        prepared_b.ok_repetitions = []
        path = tmp_path / "trend_sparse.png"
        _figure_run_order_trend([1, 2, 1, 2, 1, 2, 1, 2], 1, prepared_a, 2, prepared_b, path)
        assert path.stat().st_size > 0

    def test_tick_label_wraps_and_names_what_it_counts(self):
        prepared = _prepared(1, "Version A with a deliberately long label", [1.0, 2.0, 3.0])
        label = _tick_label(prepared, "cpu_values")
        assert label.endswith("(3 repetitions)")
        assert "\n" in label

    def test_tick_label_singularizes(self):
        prepared = _prepared(1, "A", [1.0])
        assert _tick_label(prepared, "cpu_values").endswith("(1 repetition)")

    def test_tick_label_width_stays_bounded_without_whitespace_to_wrap_at(self):
        """A label with no spaces must still not overflow into the neighbouring bar."""
        prepared = _prepared(1, "InvenTree_DEBUG=True_cache=False_verylongtoken", [1.0, 2.0])
        widest = max(len(line) for line in _tick_label(prepared, "cpu_values").splitlines())
        assert widest <= max(TICK_LABEL_WRAP_WIDTH, len("(2 repetitions)"))

    @pytest.mark.parametrize("label", ["", "   "])
    def test_tick_label_falls_back_to_app_version_id_when_label_is_blank(self, label):
        """A blank label wraps to nothing, which would leave both bars identified only by their
        repetition count. `label` is `nullable=False` but an empty string satisfies that."""
        prepared = _prepared(7, label, [1.0, 2.0])
        assert "app_version 7" in _tick_label(prepared, "cpu_values")


class TestThreadBreakdown:
    def test_thread_name_strips_uninitialized_bytes(self):
        assert _thread_name({"executable_name": "gunicorn"}) == "gunicorn"
        assert _thread_name({"executable_name": "gunicorn \x00\x1f"}) == "gunicorn"
        assert _thread_name({"executable_name": ""}) == "unknown"

    def test_parse_thread_energy_is_scoped_to_one_role(self, tmp_path):
        path = tmp_path / "raw_cpu_x.csv"
        server = "/system.slice/docker-abc123def456xx.scope"
        _write_detailed_export(
            path,
            [
                {
                    "resource_group_name": server,
                    "executable_name": "gunicorn",
                    "total_energy_j": 4.0,
                },
                {
                    "resource_group_name": server,
                    "executable_name": "ThreadPoolExecu",
                    "total_energy_j": 6.0,
                },
                {
                    "resource_group_name": "/system.slice/docker-fedcba987654xx.scope",
                    "executable_name": "postgres",
                    "total_energy_j": 9.0,
                },
            ],
        )
        container_map = {"abc123def456": "inventree-server", "fedcba987654": "inventree-db"}

        totals = _parse_thread_energy(path, container_map, "inventree-server")

        assert totals == {"gunicorn": 4.0, "ThreadPoolExecu": 6.0}

    def test_significant_groups_bucket_the_spurious_tail(self):
        """The corrupted-`comm` variants METRION produces are always tiny, so a share threshold
        removes them where a fixed top-N would keep them as their own bars."""
        totals = {
            "ThreadPoolExecu": 23.1,
            "gunicorn": 12.55,
            "gunicornB": 0.24,
            'gunicorn"': 0.15,
            "gunicornt": 0.11,
        }
        result = _significant_with_other(totals)
        assert set(result) == {"ThreadPoolExecu", "gunicorn", "other threads"}
        assert result["other threads"] == pytest.approx(0.50)
        assert sum(result.values()) == pytest.approx(sum(totals.values()))

    def test_all_significant_groups_are_kept(self):
        totals = {"a": 5.0, "b": 5.0}
        assert _significant_with_other(totals) == {"a": 5.0, "b": 5.0}

    def test_top_roles_ranks_by_combined_energy_across_versions(self):
        breakdown_a = {"server": 30.0, "worker": 14.0, "proxy": 2.0, "cache": 1.6, "db": 1.4}
        breakdown_b = {"server": 41.0, "worker": 13.0, "proxy": 1.9, "cache": 1.2, "db": 1.3}
        assert _top_roles(breakdown_a, breakdown_b, limit=4) == [
            "server",
            "worker",
            "proxy",
            "cache",
        ]

    def test_top_roles_tolerates_fewer_roles_than_the_limit(self):
        assert _top_roles({"server": 1.0}, None, limit=4) == ["server"]
        assert _top_roles(None, None, limit=4) == []


class TestRepetitionContainerEnergy:
    def test_reads_map_next_to_raw_export(self, tmp_path):
        raw_path = tmp_path / "raw_cpu_x.csv"
        _write_raw_export(raw_path, [("/system.slice/docker-abc123def456xx.scope", 5.0)])
        (tmp_path / "container_map.json").write_text(
            json.dumps({"abc123def456": "inventree-server"})
        )
        rep = SimpleNamespace(raw_export_ref=str(raw_path))

        assert _repetition_container_energy(rep) == {"inventree-server": 5.0}

    def test_missing_raw_export_ref_returns_none(self):
        assert _repetition_container_energy(SimpleNamespace(raw_export_ref=None)) is None

    def test_missing_container_map_returns_none(self, tmp_path):
        raw_path = tmp_path / "raw_cpu_x.csv"
        _write_raw_export(raw_path, [("/system.slice/docker-abc123def456xx.scope", 5.0)])
        rep = SimpleNamespace(raw_export_ref=str(raw_path))

        assert _repetition_container_energy(rep) is None


class TestVersionContainerBreakdown:
    def test_averages_across_repetitions_with_map(self, tmp_path):
        ok_repetitions = []
        for index, energy in enumerate([4.0, 6.0]):
            rep_dir = tmp_path / f"rep{index}"
            rep_dir.mkdir()
            raw_path = rep_dir / "raw_cpu_x.csv"
            _write_raw_export(raw_path, [("/system.slice/docker-abc123def456xx.scope", energy)])
            (rep_dir / "container_map.json").write_text(
                json.dumps({"abc123def456": "inventree-server"})
            )
            ok_repetitions.append(SimpleNamespace(raw_export_ref=str(raw_path)))

        prepared = _prepared(1, "A", [1.0, 1.0])
        prepared.ok_repetitions = ok_repetitions

        assert _version_container_breakdown(prepared) == {"inventree-server": 5.0}

    def test_returns_none_when_no_repetition_has_map(self):
        prepared = _prepared(1, "A", [1.0])
        prepared.ok_repetitions = [SimpleNamespace(raw_export_ref=None)]

        assert _version_container_breakdown(prepared) is None


def _comparison_session_and_check(
    declared_steps: list[str] | None = None,
) -> tuple[ComparisonSession, EquivalenceCheck]:
    """A session with a **real** `Scenario` attached.

    `build_summary` must not tolerate an absent scenario, since that would turn an unreachable
    scenario row into a silently different step order rather than a failure.
    """
    comparison_session = ComparisonSession(
        id=1,
        scenario=Scenario(id=1, name="Test Scenario", steps=declared_steps or []),
        scenario_id=1,
        host_descriptor="workstation1",
        metrion_config_path="/x/config.yaml",
        idle_power_calibration={"package_0": 5.0},
        calibration_timestamp=datetime.now(UTC),
        execution_order=[1, 2],
        random_seed=42,
    )
    equivalence_check = EquivalenceCheck(
        id=1,
        scenario_id=1,
        version_a_id=1,
        version_b_id=2,
        verdict=EquivalenceVerdict.PASS,
        notes=None,
        checked_at=datetime.now(UTC),
    )
    return comparison_session, equivalence_check


class TestBuildSummaryRequiresItsScenario:
    """An unreachable scenario row is a fault, not a shape to absorb.

    The two cases below look alike and are not: no scenario at all is a broken session, while a
    scenario declaring no steps is a legitimate one. The old guard collapsed both into "sort the
    steps and carry on", which is why the distinction is pinned here. The ordering property itself
    lives with `_ordered_steps`, which is the level it belongs at.
    """

    def test_an_unreachable_scenario_is_refused_rather_than_reordered(self):
        comparison_session, equivalence_check = _comparison_session_and_check()
        comparison_session.scenario = None
        prepared_a = _prepared(1, "A", [10.0, 10.5])
        prepared_b = _prepared(2, "B", [12.0, 12.5])

        with pytest.raises(EvaluationError, match="no reachable scenario row"):
            build_summary(comparison_session, equivalence_check, prepared_a, prepared_b)

    def test_a_scenario_declaring_no_steps_is_still_fine(self):
        """The legitimate empty case, which must not be swept up by the refusal above."""
        comparison_session, equivalence_check = _comparison_session_and_check(declared_steps=[])
        prepared_a = _prepared(1, "A", [10.0, 10.5])
        prepared_b = _prepared(2, "B", [12.0, 12.5])

        summary = build_summary(comparison_session, equivalence_check, prepared_a, prepared_b)

        assert summary["step_order"] == []


class TestBuildSummary:
    def test_structure_and_baseline_convention(self):
        comparison_session, equivalence_check = _comparison_session_and_check()
        comparison_session.execution_order = [1, 2, 1, 2, 1, 2, 1, 2]
        prepared_a = _prepared(1, "A", [10.0, 10.5, 9.5, 10.2])
        prepared_b = _prepared(2, "B", [12.0, 12.5, 11.5, 12.2])

        summary = build_summary(comparison_session, equivalence_check, prepared_a, prepared_b)

        assert summary["scenario_id"] == 1
        assert summary["comparison_session_id"] == 1
        assert summary["equivalence_check"]["verdict"] == "PASS"
        assert summary["comparison"]["baseline"] == "a"
        cpu = summary["comparison"]["cpu_energy_j"]
        assert cpu["difference_j"] == pytest.approx(2.0)
        assert cpu["relative_difference"]["method"] == "fieller"
        assert cpu["order_adjusted"]["difference_j"] is not None
        assert summary["versions"]["a"]["label"] == "A"
        assert summary["versions"]["a"]["cpu_energy_j"]["n"] == 4
        assert summary["versions"]["a"]["n_scheduled"] == 4

    def test_one_record_per_usable_repetition_in_slot_order(self):
        comparison_session, equivalence_check = _comparison_session_and_check()
        comparison_session.execution_order = [2, 1, 2, 1]
        summary = build_summary(
            comparison_session,
            equivalence_check,
            _prepared(1, "A", [10.0, 10.5]),
            _prepared(2, "B", [12.0, 12.5], noted_repetition_ids=(2,)),
        )

        records = summary["repetitions"]
        assert [(r["slot"], r["version"], r["repetition_index"]) for r in records] == [
            (0, "b", 0),
            (1, "a", 0),
            (2, "b", 1),
            (3, "a", 1),
        ]
        assert records[2]["attempt_count"] == 1
        assert records[2]["validation_notes"].startswith("attempt 1 failed")
        assert records[0]["validation_notes"] is None
        assert records[0]["start_time"] == "2026-01-01T12:00:00+00:00"
        assert records[0]["end_time"] == "2026-01-01T12:01:00+00:00"
        assert records[0]["measurement_started_at"] is None
        assert summary["versions"]["b"]["n_noted"] == 1

    def test_dram_omitted_when_not_reported(self):
        comparison_session, equivalence_check = _comparison_session_and_check()
        prepared_a = _prepared(1, "A", [10.0, 10.5, 9.5])
        prepared_b = _prepared(2, "B", [12.0, 12.5, 11.5])

        summary = build_summary(comparison_session, equivalence_check, prepared_a, prepared_b)

        assert summary["versions"]["a"]["dram_energy_j"] is None
        assert summary["comparison"]["dram_energy_j"] is None

    def test_dram_gets_the_same_comparison_when_both_versions_measured_it(self):
        comparison_session, equivalence_check = _comparison_session_and_check()
        comparison_session.execution_order = [1, 2, 1, 2, 1, 2]
        summary = build_summary(
            comparison_session,
            equivalence_check,
            _prepared(1, "A", [10.0, 10.5, 9.5], dram_values=[1.0, 1.1, 0.9]),
            _prepared(2, "B", [12.0, 12.5, 11.5], dram_values=[1.3, 1.4, 1.2]),
        )

        dram = summary["comparison"]["dram_energy_j"]
        assert dram["difference_j"] == pytest.approx(0.3)
        assert summary["versions"]["b"]["dram_energy_j"]["mean"] == pytest.approx(1.3)

    def test_the_summary_states_k(self):
        """A headline number is energy per scenario execution of k iterations."""
        comparison_session, equivalence_check = _comparison_session_and_check()
        summary = build_summary(
            comparison_session,
            equivalence_check,
            _prepared(1, "A", [10.0, 10.5]),
            _prepared(2, "B", [12.0, 12.5]),
        )

        assert "scenario_iterations" in summary

    def test_a_scenario_without_a_repeated_block_has_no_per_iteration_summary(self):
        comparison_session, equivalence_check = _comparison_session_and_check()
        summary = build_summary(
            comparison_session,
            equivalence_check,
            _prepared(1, "A", [10.0, 10.5]),
            _prepared(2, "B", [12.0, 12.5]),
        )

        assert summary["per_iteration"] is None


class TestSessionContextGovernor:
    """The recorded CPU governor reaches the report, which says so when none was recorded."""

    def _report(self, cpu_governor):
        comparison_session, equivalence_check = _comparison_session_and_check()
        comparison_session.cpu_governor = cpu_governor
        prepared_a = _prepared(1, "A", [10.0, 10.5, 9.5, 10.2])
        prepared_b = _prepared(2, "B", [12.0, 12.5, 11.5, 12.2])
        summary = build_summary(comparison_session, equivalence_check, prepared_a, prepared_b)
        assert summary["session_context"]["cpu_governor"] == cpu_governor
        return render_report(summary, {"fig1": "f1.png", "fig2": "f2.png", "fig3": "f3.png"})

    def test_a_recorded_governor_is_shown(self):
        assert "| CPU governor | `performance` |" in self._report("performance")

    def test_a_mixed_reading_is_shown_as_read(self):
        """It should never occur, since the pre-session check blocks on it, but if a row ever
        carries it the report must not tidy it into a single governor."""
        assert "`performance,powersave`" in self._report("performance,powersave")

    def test_an_unrecorded_governor_says_so_rather_than_rendering_blank(self):
        """A blank cell reads as "no governor"; the truth for older sessions is "not recorded"."""
        report = self._report(None)
        assert "| CPU governor | not recorded |" in report


class TestSessionContextHostFacts:
    """Section 1 shows a line of host facts, not the blob."""

    FACTS = {
        "cpu_model": "Intel(R) Core(TM) Ultra 9 285K",
        "cpu_count": 24,
        "ram_bytes": 65426362368,
        "kernel_release": "7.0.0-29-generic",
        "distro": "Ubuntu 26.04 LTS",
        "virtualization": "none",
        "rapl_domains": ["intel-rapl", "intel-rapl:0"],
        "python_version": "3.12.14",
        "docker_version": "Docker version 29.7.2, build a7dcaa6",
        "metrion_version": "metrion-0.1.0",
        "metrion_package_sha256": (
            "79579590da52e3478ff4d0f5a04ae4b043edf12d645754e7550dc954d744c4d2"
        ),
    }

    def _report(self, host_facts):
        comparison_session, equivalence_check = _comparison_session_and_check()
        comparison_session.host_facts = host_facts
        prepared_a = _prepared(1, "A", [10.0, 10.5, 9.5, 10.2])
        prepared_b = _prepared(2, "B", [12.0, 12.5, 11.5, 12.2])
        summary = build_summary(comparison_session, equivalence_check, prepared_a, prepared_b)
        return render_report(summary, {"fig1": "f1.png", "fig2": "f2.png", "fig3": "f3.png"})

    def test_hardware_line_reads_as_one_sentence(self):
        report = self._report(self.FACTS)
        assert (
            "| Host hardware and OS | Intel(R) Core(TM) Ultra 9 285K, 24 cores, "
            "kernel 7.0.0-29-generic, Ubuntu 26.04 LTS, bare metal |" in report
        )

    def test_virtualization_none_is_rendered_as_bare_metal(self):
        """ "none" is `systemd-detect-virt`'s word for it and reads as missing data in a table."""
        assert "bare metal" in self._report(self.FACTS)
        assert "| Host hardware and OS | " in self._report({**self.FACTS, "virtualization": "kvm"})
        assert "kvm" in self._report({**self.FACTS, "virtualization": "kvm"})

    def test_metrion_hash_is_truncated_to_a_comparable_prefix(self):
        report = self._report(self.FACTS)
        assert "`metrion-0.1.0`, package `79579590da52e347`" in report
        assert self.FACTS["metrion_package_sha256"] not in report

    def test_the_blob_itself_is_not_dumped_into_section_1(self):
        """Section 1 is orientation; RAM and the RAPL domain list live in the column."""
        report = self._report(self.FACTS)
        assert "65426362368" not in report
        assert "intel-rapl:0" not in report

    def test_a_session_without_host_facts_says_not_recorded(self):
        report = self._report(None)
        assert "| Host hardware and OS | not recorded |" in report
        assert "| METRION build | not recorded |" in report

    def test_partial_facts_render_what_exists(self):
        """One unreadable fact must not blank the whole line, since readers fail individually."""
        report = self._report({"kernel_release": "7.0.0-29-generic", "cpu_model": None})
        assert "| Host hardware and OS | kernel 7.0.0-29-generic |" in report


def _summary_for_report(**overrides) -> dict:
    comparison_session, equivalence_check = _comparison_session_and_check()
    comparison_session.execution_order = [1, 2] * 8
    prepared_a = _prepared(1, "A", [10.0, 10.5, 9.5, 10.2, 9.8, 10.1, 9.9, 10.3])
    prepared_b = _prepared(
        2, "B", [12.0, 12.5, 11.5, 12.2, 11.8, 12.1, 11.9, 12.3], noted_repetition_ids=(3,)
    )
    summary = build_summary(comparison_session, equivalence_check, prepared_a, prepared_b)
    summary.update(overrides)
    return summary


_CORE_FIGURES = {"fig1": "fig1.png", "fig2": "fig2.png", "fig3": "fig3.png"}


class TestRenderReport:
    def test_sections_are_fixed_and_in_reading_order(self):
        report = render_report(_summary_for_report(), _CORE_FIGURES)

        headings = [line for line in report.splitlines() if line.startswith("## ")]
        assert headings == [
            "## 1. Setup",
            "## 2. Descriptive Statistics",
            "## 3. Comparison",
            "## 4. Per-Container Energy",
        ]

    def test_core_figures_are_embedded(self):
        report = render_report(_summary_for_report(), _CORE_FIGURES)

        for path in _CORE_FIGURES.values():
            assert f"({path})" in report

    def test_setup_counts_retried_and_noted_repetitions(self):
        report = render_report(_summary_for_report(), _CORE_FIGURES)

        assert "| Baseline (`a`) | A | 1 | 8 | 8 | 0 | 0 |" in report
        assert "| Compared (`b`) | B | 2 | 8 | 8 | 1 | 1 |" in report

    def test_comparison_reports_welch_fieller_mann_whitney_and_order_adjustment(self):
        summary = _summary_for_report()
        report = render_report(summary, _CORE_FIGURES)

        cpu = summary["comparison"]["cpu_energy_j"]
        assert f"| Difference `b` - `a` (J) | {cpu['difference_j']:+.2f} |" in report
        for label in (
            "Welch 95% CI (J)",
            "Fieller 95% CI",
            "Welch p",
            "Mann-Whitney U p",
            "Run-order-adjusted difference (J) [95% CI]",
        ):
            assert f"| {label} |" in report
        assert "| Welch p | < 0.001 |" in report

    def test_no_standardized_effect_size_or_significance_wording(self):
        report = render_report(_summary_for_report(), _CORE_FIGURES)

        for absent in ("Cohen", "Hedges", "Shapiro", "bootstrap", "distinguishable", "large"):
            assert absent not in report

    def test_dram_column_appears_only_when_both_versions_measured_it(self):
        comparison_session, equivalence_check = _comparison_session_and_check()
        comparison_session.execution_order = [1, 2, 1, 2, 1, 2]
        summary = build_summary(
            comparison_session,
            equivalence_check,
            _prepared(1, "A", [10.0, 10.5, 9.5], dram_values=[1.0, 1.1, 0.9]),
            _prepared(2, "B", [12.0, 12.5, 11.5], dram_values=[1.3, 1.4, 1.2]),
        )
        report = render_report(summary, _CORE_FIGURES)

        assert "|  | CPU energy | DRAM energy |" in report
        assert "| DRAM energy mean (J) |" in report
        assert "DRAM" not in render_report(_summary_for_report(), _CORE_FIGURES)

    def test_containers_unavailable_without_archived_exports(self):
        report = render_report(_summary_for_report(), _CORE_FIGURES)

        assert "## 4. Per-Container Energy" in report
        assert "Not available" in report

    def test_container_table_uses_the_unadjusted_welch_interval(self):
        summary = _summary_for_report()
        summary["versions"]["a"]["container_breakdown"] = {"server": 8.0, "db": 2.0}
        summary["versions"]["b"]["container_breakdown"] = {"server": 9.5, "db": 2.5}
        summary["comparison"]["container_deltas"] = [
            {"role": "server", "delta_j": 1.5, "welch_ci95_j": [1.0, 2.0]},
            {"role": "db", "delta_j": 0.5, "welch_ci95_j": [-0.1, 1.1]},
        ]
        report = render_report(summary, {**_CORE_FIGURES, "fig4": "f4.png", "fig5": "f5.png"})

        assert "| `server` | 8.00 | 9.50 | +1.50 | [+1.00, +2.00] |" in report
        assert "not adjusted for the number of containers" in report
        assert report.index("`server`") < report.index("`db`")

    def test_supplementary_figures_are_listed_with_captions_when_present(self):
        figures = {**_CORE_FIGURES, "fig8": "f8.png", "fig9": "f9.png", "fig10": "f10.png"}
        report = render_report(_summary_for_report(), figures)

        assert "## Supplementary Figures" in report
        assert "(f8.png)" in report and "(f10.png)" in report
        assert "not per-step effects" in report
        assert "## Supplementary Figures" not in render_report(_summary_for_report(), _CORE_FIGURES)

    def test_execution_order_renders_as_baseline_letters(self):
        report = render_report(_summary_for_report(), _CORE_FIGURES)

        assert "run  1-10  a b a b a b a b a b" in report

    def test_renders_undefined_metrics_as_not_available(self):
        summary = _summary_for_report()
        summary["versions"]["a"]["runtime_s"] = None
        report = render_report(summary, _CORE_FIGURES)

        assert "| Runtime mean ± SD (s) | n/a ± n/a |" in report


class TestRunEvaluation:
    def _build_comparison_session(self, session, scenario, app_versions) -> ComparisonSession:
        version_a, version_b = app_versions
        comparison_session = ComparisonSession(
            scenario_id=scenario.id,
            host_descriptor="workstation1",
            metrion_config_path="/x/config.yaml",
            idle_power_calibration={"package_0": 5.0},
            calibration_timestamp=datetime.now(UTC),
            execution_order=[version_a.id, version_b.id, version_a.id, version_b.id],
            random_seed=42,
        )
        session.add(comparison_session)
        session.flush()

        equivalence_check = EquivalenceCheck(
            scenario_id=scenario.id,
            version_a_id=version_a.id,
            version_b_id=version_b.id,
            verdict=EquivalenceVerdict.PASS,
            checked_at=comparison_session.calibration_timestamp,
        )
        session.add(equivalence_check)

        run_a = VersionRun(
            comparison_session_id=comparison_session.id,
            app_version_id=version_a.id,
            repetition_count_planned=2,
        )
        run_b = VersionRun(
            comparison_session_id=comparison_session.id,
            app_version_id=version_b.id,
            repetition_count_planned=2,
        )
        session.add_all([run_a, run_b])
        session.flush()

        for index, energy in enumerate([10.0, 10.2]):
            session.add(
                Repetition(
                    version_run_id=run_a.id,
                    repetition_index=index,
                    status=RepetitionStatus.OK,
                    cpu_energy_j=energy,
                    runtime_s=50.0,
                    average_power_w=0.2,
                )
            )
        for index, energy in enumerate([12.0, 12.4]):
            session.add(
                Repetition(
                    version_run_id=run_b.id,
                    repetition_index=index,
                    status=RepetitionStatus.OK,
                    cpu_energy_j=energy,
                    runtime_s=50.0,
                    average_power_w=0.24,
                )
            )
        session.commit()
        return comparison_session

    def test_writes_all_artifacts(self, session, scenario, app_versions, tmp_path):
        comparison_session = self._build_comparison_session(session, scenario, app_versions)

        output_dir = run_evaluation(session, comparison_session.id, output_root=tmp_path)

        # Host-qualified; the fixture's host_descriptor is "workstation1".
        assert output_dir == tmp_path / "workstation1" / f"{scenario.id}_{comparison_session.id}"
        for name in (
            "summary.json",
            "comparison_report.md",
            "fig1_energy_comparison.png",
            "fig1_energy_comparison.pdf",
            "fig2_energy_distribution.png",
            "fig3_run_order_trend.png",
        ):
            assert (output_dir / name).exists()
        summary = json.loads((output_dir / "summary.json").read_text())
        assert summary["comparison"]["baseline"] == "a"
        assert summary["versions"]["a"]["n_ok"] == 2
        assert summary["versions"]["b"]["n_ok"] == 2
        assert summary["versions"]["a"]["container_breakdown"] is None
        assert not (output_dir / "fig4_container_breakdown.png").exists()
        assert "Not available" in (output_dir / "comparison_report.md").read_text()

    def test_includes_container_breakdown_when_available(
        self, session, scenario, app_versions, tmp_path
    ):
        version_a, version_b = app_versions
        comparison_session = ComparisonSession(
            scenario_id=scenario.id,
            host_descriptor="workstation1",
            metrion_config_path="/x/config.yaml",
            idle_power_calibration={"package_0": 5.0},
            calibration_timestamp=datetime.now(UTC),
            execution_order=[version_a.id, version_b.id],
            random_seed=1,
        )
        session.add(comparison_session)
        session.flush()
        equivalence_check = EquivalenceCheck(
            scenario_id=scenario.id,
            version_a_id=version_a.id,
            version_b_id=version_b.id,
            verdict=EquivalenceVerdict.PASS,
            checked_at=comparison_session.calibration_timestamp,
        )
        session.add(equivalence_check)
        run_a = VersionRun(
            comparison_session_id=comparison_session.id,
            app_version_id=version_a.id,
            repetition_count_planned=1,
        )
        run_b = VersionRun(
            comparison_session_id=comparison_session.id,
            app_version_id=version_b.id,
            repetition_count_planned=1,
        )
        session.add_all([run_a, run_b])
        session.flush()

        server = "/system.slice/docker-abc123def456xx.scope"
        for run, gunicorn_j, pool_j in ((run_a, 4.0, 6.0), (run_b, 4.4, 7.6)):
            for index in range(2):
                rep_dir = tmp_path / f"rep_{run.app_version_id}_{index}"
                rep_dir.mkdir()
                raw_path = rep_dir / "raw_cpu_x.csv"
                _write_detailed_export(
                    raw_path,
                    [
                        {
                            "resource_group_name": server,
                            "executable_name": name,
                            "total_energy_j": value + index * 0.1,
                            "active_energy_j": (value + index * 0.1) * 0.8,
                            "idle_energy_j": (value + index * 0.1) * 0.2,
                        }
                        for name, value in (("gunicorn", gunicorn_j), ("ThreadPoolExecu", pool_j))
                    ],
                )
                (rep_dir / "container_map.json").write_text(
                    json.dumps({"abc123def456": "inventree-server"})
                )
                session.add(
                    Repetition(
                        version_run_id=run.id,
                        repetition_index=index,
                        status=RepetitionStatus.OK,
                        cpu_energy_j=gunicorn_j + pool_j + index * 0.2,
                        runtime_s=50.0,
                        average_power_w=0.2,
                        raw_export_ref=str(raw_path),
                    )
                )
        session.commit()

        output_dir = run_evaluation(session, comparison_session.id, output_root=tmp_path / "out")

        for name in (
            "fig4_container_breakdown.png",
            "fig5_container_deltas.png",
            "fig6_energy_split.png",
            "fig7_thread_breakdown.png",
        ):
            assert (output_dir / name).exists(), name
        summary = json.loads((output_dir / "summary.json").read_text())
        assert summary["versions"]["a"]["container_breakdown"] == {"inventree-server": 10.1}
        assert summary["versions"]["b"]["container_breakdown"] == {"inventree-server": 12.1}
        assert summary["thread_breakdown_containers"] == ["inventree-server"]

        # Active/idle is a decomposition of the tested total, so it must sum back to it.
        split = summary["versions"]["a"]["energy_split"]
        assert split["reconciles"]
        assert split["active_j"]["mean"] + split["idle_j"]["mean"] == pytest.approx(10.1)
        assert split["active_share"] == pytest.approx(0.8)

        deltas = summary["comparison"]["container_deltas"]
        assert [entry["role"] for entry in deltas] == ["inventree-server"]
        assert deltas[0]["delta_j"] == pytest.approx(2.0)

        report = (output_dir / "comparison_report.md").read_text()
        assert "inventree-server" in report
        assert "## 4. Per-Container Energy" in report
        assert "(fig7_thread_breakdown.png)" in report
        assert "(fig6_energy_split.png)" in report

    def test_missing_comparison_session_raises(self, session):
        with pytest.raises(EvaluationError, match="no comparison_session"):
            run_evaluation(session, comparison_session_id=999, output_root=Path("/tmp"))

    def test_missing_equivalence_check_raises(self, session, scenario, app_versions):
        version_a, version_b = app_versions
        comparison_session = ComparisonSession(
            scenario_id=scenario.id,
            host_descriptor="workstation1",
            metrion_config_path="/x/config.yaml",
            idle_power_calibration={},
            calibration_timestamp=datetime.now(UTC),
            execution_order=[version_a.id],
            random_seed=1,
        )
        session.add(comparison_session)
        session.flush()
        run_a = VersionRun(
            comparison_session_id=comparison_session.id,
            app_version_id=version_a.id,
            repetition_count_planned=1,
        )
        run_b = VersionRun(
            comparison_session_id=comparison_session.id,
            app_version_id=version_b.id,
            repetition_count_planned=1,
        )
        session.add_all([run_a, run_b])
        session.commit()

        with pytest.raises(EvaluationError, match="no equivalence_check"):
            run_evaluation(session, comparison_session.id, output_root=Path("/tmp"))


class TestPerStepDecomposition:
    def test_steps_are_ordered_by_the_scenario_not_by_name_or_size(self):
        """Containers are a set and steps are a sequence, so reordering them would break the
        correspondence to the step log."""
        declared = ["create_part", "adjust_stock", "browse_catalog", "run_build_order"]
        breakdown = {"run_build_order": 60.0, "adjust_stock": 17.0, "create_part": 10.0}

        assert _ordered_steps(declared, breakdown) == [
            "create_part",
            "adjust_stock",
            "run_build_order",
        ]

    def test_a_step_the_scenario_does_not_declare_is_appended_not_dropped(self):
        """A step that reached a step log is energy that was really spent."""
        assert _ordered_steps(["a", "b"], {"b": 1.0, "zz": 2.0, "a": 3.0}) == ["a", "b", "zz"]

    def test_without_a_declared_list_it_falls_back_to_a_deterministic_order(self):
        assert _ordered_steps([], {"b": 1.0, "a": 2.0}) == ["a", "b"]


class TestHostSlug:
    """`comparison_session.host_descriptor` is free text and becomes a directory name."""

    @pytest.mark.parametrize(
        ("descriptor", "expected"),
        [
            ("workstation1", "workstation1"),
            ("workstation2", "workstation2"),
            ("lab host 2", "lab-host-2"),
            ("host.example.org", "host.example.org"),
            ("  padded  ", "padded"),
        ],
    )
    def test_keeps_ordinary_descriptors_readable(self, descriptor, expected):
        assert _host_slug(descriptor) == expected

    @pytest.mark.parametrize("descriptor", ["../../etc", "a/b", "/", "..", "  ", "", None])
    def test_never_escapes_one_path_component(self, descriptor):
        """The reason this function exists: a slash or a `..` would write outside output_root."""
        slug = _host_slug(descriptor)
        assert slug
        assert "/" not in slug
        assert slug not in (".", "..")
        assert Path(slug).name == slug


class TestRunEvaluationOutputDir:
    """The path is host-qualified and never overwritten.

    Borrows `TestRunEvaluation`'s builder by delegation rather than by inheriting it, which
    would re-run that class's four tests under a second name for nothing.
    """

    def _build_comparison_session(self, session, scenario, app_versions) -> ComparisonSession:
        return TestRunEvaluation()._build_comparison_session(session, scenario, app_versions)

    def test_two_hosts_with_the_same_ids_do_not_collide(
        self, session, scenario, app_versions, tmp_path
    ):
        """Same scenario id, same session id, two hosts: the outputs must not collide."""
        first = self._build_comparison_session(session, scenario, app_versions)
        first_dir = run_evaluation(session, first.id, output_root=tmp_path)

        second = self._build_comparison_session(session, scenario, app_versions)
        second.host_descriptor = "workstation2"
        session.commit()
        second_dir = run_evaluation(session, second.id, output_root=tmp_path)

        assert first_dir != second_dir
        assert first_dir.parent.name == "workstation1"
        assert second_dir.parent.name == "workstation2"
        # and the first report is still the first report
        assert "workstation1" in (first_dir / "comparison_report.md").read_text()

    def test_refuses_to_overwrite_a_non_empty_directory(
        self, session, scenario, app_versions, tmp_path
    ):
        comparison_session = self._build_comparison_session(session, scenario, app_versions)
        output_dir = run_evaluation(session, comparison_session.id, output_root=tmp_path)
        before = (output_dir / "comparison_report.md").read_text()

        with pytest.raises(EvaluationError, match="refusing to overwrite"):
            run_evaluation(session, comparison_session.id, output_root=tmp_path)

        assert (output_dir / "comparison_report.md").read_text() == before

    def test_an_empty_directory_is_not_an_obstacle(self, session, scenario, app_versions, tmp_path):
        """Refusing on mere existence would make a stray mkdir unrecoverable without a delete."""
        comparison_session = self._build_comparison_session(session, scenario, app_versions)
        (tmp_path / "workstation1" / f"{scenario.id}_{comparison_session.id}").mkdir(parents=True)

        output_dir = run_evaluation(session, comparison_session.id, output_root=tmp_path)

        assert (output_dir / "comparison_report.md").exists()
