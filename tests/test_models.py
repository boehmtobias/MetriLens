from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from transitions.core import MachineError

from metrilens.models import (
    ComparisonSession,
    EquivalenceCheck,
    EquivalenceVerdict,
    Repetition,
    RepetitionStatus,
    Scenario,
    VersionRun,
)


class TestRepetitionStateMachine:
    def test_initial_state_is_running(self, version_run):
        rep = Repetition(version_run_id=version_run.id, repetition_index=0)
        assert rep.status is RepetitionStatus.RUNNING
        assert rep.attempt_count == 0

    def test_complete_transitions_to_ok(self, version_run):
        rep = Repetition(version_run_id=version_run.id, repetition_index=0)
        rep.complete()
        assert rep.status is RepetitionStatus.OK

    @pytest.mark.parametrize(
        ("trigger", "expected_status"),
        [
            ("fail_validation", RepetitionStatus.FAILED_VALIDATION),
            ("fail_measurement", RepetitionStatus.FAILED_MEASUREMENT),
        ],
    )
    def test_failure_transitions(self, version_run, trigger, expected_status):
        rep = Repetition(version_run_id=version_run.id, repetition_index=0)
        getattr(rep, trigger)()
        assert rep.status is expected_status

    @pytest.mark.parametrize("trigger", ["fail_validation", "fail_measurement"])
    def test_retry_returns_to_running_and_increments_attempt_count(self, version_run, trigger):
        rep = Repetition(version_run_id=version_run.id, repetition_index=0)
        getattr(rep, trigger)()
        rep.retry()
        assert rep.status is RepetitionStatus.RUNNING
        assert rep.attempt_count == 1
        rep.fail_validation()
        rep.retry()
        assert rep.attempt_count == 2

    def test_illegal_transition_raises(self, version_run):
        rep = Repetition(version_run_id=version_run.id, repetition_index=0)
        rep.complete()
        with pytest.raises(MachineError):
            rep.fail_measurement()
        assert rep.status is RepetitionStatus.OK

    def test_retry_from_ok_is_illegal(self, version_run):
        rep = Repetition(version_run_id=version_run.id, repetition_index=0)
        rep.complete()
        with pytest.raises(MachineError):
            rep.retry()

    def test_state_survives_db_reload(self, session, version_run):
        rep = Repetition(version_run_id=version_run.id, repetition_index=0)
        rep.fail_measurement()
        session.add(rep)
        session.commit()
        rep_id = rep.id

        session.expire_all()
        loaded = session.get(Repetition, rep_id)

        assert loaded.status is RepetitionStatus.FAILED_MEASUREMENT
        loaded.retry()
        assert loaded.status is RepetitionStatus.RUNNING
        assert loaded.attempt_count == 1
        with pytest.raises(MachineError):
            loaded.retry()


class TestRepetitionConstraints:
    def test_negative_cpu_energy_rejected(self, session, version_run):
        rep = Repetition(version_run_id=version_run.id, repetition_index=0, cpu_energy_j=-1.0)
        session.add(rep)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_zero_runtime_rejected(self, session, version_run):
        rep = Repetition(version_run_id=version_run.id, repetition_index=0, runtime_s=0.0)
        session.add(rep)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_end_time_before_start_time_rejected(self, session, version_run):
        start = datetime.now(UTC)
        rep = Repetition(
            version_run_id=version_run.id,
            repetition_index=0,
            start_time=start,
            end_time=start - timedelta(seconds=1),
        )
        session.add(rep)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_valid_energy_values_accepted(self, session, version_run):
        start = datetime.now(UTC)
        rep = Repetition(
            version_run_id=version_run.id,
            repetition_index=0,
            cpu_energy_j=1.5,
            dram_energy_j=0.0,
            runtime_s=5.0,
            start_time=start,
            end_time=start + timedelta(seconds=5),
        )
        session.add(rep)
        session.commit()
        assert rep.id is not None


class TestVersionRunConstraints:
    def test_zero_repetition_count_planned_rejected(self, session, scenario, app_versions):
        version_a, _ = app_versions
        comparison_session = ComparisonSession(
            scenario_id=scenario.id,
            host_descriptor="workstation1",
            metrion_config_path="c.yaml",
            idle_power_calibration={},
            calibration_timestamp=datetime.now(UTC),
            execution_order=[version_a.id],
            random_seed=1,
        )
        session.add(comparison_session)
        session.flush()
        bad_run = VersionRun(
            comparison_session_id=comparison_session.id,
            app_version_id=version_a.id,
            repetition_count_planned=0,
        )
        session.add(bad_run)
        with pytest.raises(IntegrityError):
            session.commit()


class TestEquivalenceCheckStateMachine:
    def test_initial_state_is_pending(self, scenario, app_versions):
        version_a, version_b = app_versions
        eq = EquivalenceCheck(
            scenario_id=scenario.id, version_a_id=version_a.id, version_b_id=version_b.id
        )
        assert eq.verdict is EquivalenceVerdict.PENDING

    def test_pass_check_transitions_to_pass(self, scenario, app_versions):
        version_a, version_b = app_versions
        eq = EquivalenceCheck(
            scenario_id=scenario.id, version_a_id=version_a.id, version_b_id=version_b.id
        )
        eq.pass_check()
        assert eq.verdict is EquivalenceVerdict.PASS

    def test_flag_requires_notes(self, scenario, app_versions):
        version_a, version_b = app_versions
        eq = EquivalenceCheck(
            scenario_id=scenario.id, version_a_id=version_a.id, version_b_id=version_b.id
        )
        with pytest.raises(ValueError, match="notes"):
            eq.flag()
        assert eq.verdict is EquivalenceVerdict.PENDING

    def test_flag_then_override_with_notes(self, scenario, app_versions):
        version_a, version_b = app_versions
        eq = EquivalenceCheck(
            scenario_id=scenario.id, version_a_id=version_a.id, version_b_id=version_b.id
        )
        eq.flag(notes="step_count mismatch: 3 vs 4")
        assert eq.verdict is EquivalenceVerdict.FLAGGED
        assert eq.notes == "step_count mismatch: 3 vs 4"

        eq.override(notes="UI difference accepted")
        assert eq.verdict is EquivalenceVerdict.PASS_OVERRIDDEN
        assert eq.notes == "UI difference accepted"

    def test_pass_check_after_flag_is_illegal(self, scenario, app_versions):
        version_a, version_b = app_versions
        eq = EquivalenceCheck(
            scenario_id=scenario.id, version_a_id=version_a.id, version_b_id=version_b.id
        )
        eq.flag(notes="mismatch")
        with pytest.raises(MachineError):
            eq.pass_check()

    def test_state_survives_db_reload(self, session, scenario, app_versions):
        version_a, version_b = app_versions
        eq = EquivalenceCheck(
            scenario_id=scenario.id, version_a_id=version_a.id, version_b_id=version_b.id
        )
        eq.flag(notes="mismatch")
        session.add(eq)
        session.commit()
        eq_id = eq.id

        session.expire_all()
        loaded = session.get(EquivalenceCheck, eq_id)

        assert loaded.verdict is EquivalenceVerdict.FLAGGED
        loaded.override(notes="accepted on reload")
        assert loaded.verdict is EquivalenceVerdict.PASS_OVERRIDDEN


class TestRelationships:
    def test_scenario_app_versions_relationship(self, scenario, app_versions):
        version_a, version_b = app_versions
        assert {v.label for v in scenario.app_versions} == {"DEBUG=True", "DEBUG=False"}
        assert version_a.scenario is scenario

    def test_scenario_requires_name(self, session):
        with pytest.raises(IntegrityError):
            session.add(Scenario(name=None))
            session.commit()


class TestScenarioIterations:
    """k, the number of passes through the repeated step block inside one repetition, belongs
    to the scenario definition rather than to a run: a headline number means energy per scenario
    execution *of k iterations*, and every version being compared must run the same k."""

    def test_it_defaults_to_one(self, session):
        """1 is not a placeholder for scenarios written before this column existed, it is the
        correct value for a scenario whose step block runs exactly once."""
        scenario = Scenario(name="single pass", steps=["a"])
        session.add(scenario)
        session.flush()
        assert scenario.iterations == 1

    def test_it_round_trips(self, session):
        scenario = Scenario(name="three passes", steps=["a"], iterations=3)
        session.add(scenario)
        session.commit()
        session.expunge_all()
        assert session.query(Scenario).filter_by(name="three passes").one().iterations == 3

    def test_an_explicit_none_falls_back_to_the_default(self, session):
        """Not the not-null violation it looks like it should be: SQLAlchemy applies a column
        default whenever the value is None at insert time, so the column cannot be nulled
        through the ORM at all. Recorded as a test because the first version of it asserted
        the opposite and was wrong."""
        scenario = Scenario(name="null k", steps=["a"], iterations=None)
        session.add(scenario)
        session.flush()
        assert scenario.iterations == 1
