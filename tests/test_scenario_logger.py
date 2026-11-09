import json
from datetime import UTC, datetime

import pytest

from metrilens.scenario_logger import ScenarioLogger, ScenarioLoggerError

STEPS = ["create_part", "adjust_stock", "run_build_order"]


class TestTimeBase:
    """Every timestamp is timezone-aware UTC.

    `writer.py` copies these values onto `repetition.start_time`/`end_time`. A regression to naive
    local time would be silent and would only surface once an `analyze` window is derived from
    this log, where a two-hour offset yields an empty window rather than an error.
    """

    def _step_log(self, tmp_path) -> dict:
        logger = ScenarioLogger(STEPS, tmp_path / "log.json")
        with logger.step("create_part"):
            logger.action()
        return logger.finalize()

    def test_every_timestamp_is_offset_aware(self, tmp_path):
        step_log = self._step_log(tmp_path)
        stamps = [step_log["start_time"], step_log["end_time"]]
        stamps += [step_log["steps"][0]["start_time"], step_log["steps"][0]["end_time"]]
        for stamp in stamps:
            assert datetime.fromisoformat(stamp).tzinfo is not None

    def test_timestamps_are_utc_not_a_local_offset(self, tmp_path):
        step_log = self._step_log(tmp_path)
        parsed = datetime.fromisoformat(step_log["start_time"])
        assert parsed.utcoffset().total_seconds() == 0

    def test_timestamps_agree_with_the_pipeline_s_own_clock(self, tmp_path):
        # A step-log timestamp compared against a `datetime.now(UTC)` taken by the
        # orchestrator moments earlier.
        before = datetime.now(UTC)
        step_log = self._step_log(tmp_path)
        after = datetime.now(UTC)
        assert before <= datetime.fromisoformat(step_log["start_time"]) <= after


class TestStep:
    def test_undeclared_step_raises(self, tmp_path):
        logger = ScenarioLogger(STEPS, tmp_path / "log.json")
        with pytest.raises(ScenarioLoggerError, match="not declared"):
            with logger.step("typo_step"):
                pass

    def test_nested_step_raises(self, tmp_path):
        logger = ScenarioLogger(STEPS, tmp_path / "log.json")
        with pytest.raises(ScenarioLoggerError, match="still open"):
            with logger.step("create_part"):
                with logger.step("adjust_stock"):
                    pass

    def test_can_reopen_step_after_it_closes(self, tmp_path):
        logger = ScenarioLogger(STEPS, tmp_path / "log.json")
        with logger.step("create_part"):
            pass
        with logger.step("adjust_stock"):
            pass


class TestActionAndEntityCreated:
    @pytest.mark.parametrize(
        "call", [lambda logger: logger.action(), lambda logger: logger.entity_created("part")]
    )
    def test_outside_step_raises(self, tmp_path, call):
        logger = ScenarioLogger(STEPS, tmp_path / "log.json")
        with pytest.raises(ScenarioLoggerError, match="outside an active step"):
            call(logger)

    def test_action_and_entity_counted_within_step(self, tmp_path):
        logger = ScenarioLogger(STEPS, tmp_path / "log.json")
        with logger.step("create_part"):
            logger.action()
            logger.action()
            logger.entity_created("part", identifier="WIDGET-001")

        step_log = logger.finalize()
        assert step_log["steps"][0]["action_count"] == 2
        assert step_log["steps"][0]["entities"] == [
            {"entity_type": "part", "identifier": "WIDGET-001"}
        ]


class TestFinalize:
    def test_no_steps_raises(self, tmp_path):
        logger = ScenarioLogger(STEPS, tmp_path / "log.json")
        with pytest.raises(ScenarioLoggerError, match="no steps logged"):
            logger.finalize()

    def test_writes_ordered_log_with_aggregate_counts(self, tmp_path):
        output_path = tmp_path / "nested" / "log.json"
        logger = ScenarioLogger(STEPS, output_path)

        with logger.step("create_part"):
            logger.action()
            logger.entity_created("part")
        with logger.step("adjust_stock"):
            logger.action()
            logger.action()

        step_log = logger.finalize()

        assert step_log["step_count"] == 2
        assert step_log["action_count"] == 3
        assert step_log["entity_count"] == 1
        assert [s["step_id"] for s in step_log["steps"]] == ["create_part", "adjust_stock"]
        assert step_log["start_time"] <= step_log["end_time"]

        assert output_path.exists()
        assert json.loads(output_path.read_text()) == step_log


class TestIterations:
    """An *iteration* is one pass through the scenario's repeated step block inside a
    single repetition; a *repetition* stays one measured execution of the whole scenario. A step
    id therefore appears once per iteration in one log, which is why the declared step list is a
    vocabulary rather than an expected sequence.
    """

    def test_a_step_records_the_iteration_it_belongs_to(self, tmp_path):
        logger = ScenarioLogger(["create_part", "browse_catalog"], tmp_path / "log.json")
        for iteration in (1, 2):
            with logger.step("create_part", iteration=iteration):
                logger.action()
            with logger.step("browse_catalog", iteration=iteration):
                logger.action()

        log = logger.finalize()

        assert [step["iteration"] for step in log["steps"]] == [1, 1, 2, 2]
        assert [step["step_id"] for step in log["steps"]] == [
            "create_part",
            "browse_catalog",
            "create_part",
            "browse_catalog",
        ]
        assert log["step_count"] == 4
        assert log["iteration_count"] == 2

    def test_a_step_outside_the_block_carries_no_iteration(self, tmp_path):
        """`run_build_order` runs once per repetition rather than once per iteration, because its
        sigma is a floor and repeating it would triple the noise it contributes for no extra
        signal."""
        logger = ScenarioLogger(["create_part", "run_build_order"], tmp_path / "log.json")
        with logger.step("create_part", iteration=1):
            logger.action()
        with logger.step("run_build_order"):
            logger.action()

        log = logger.finalize()

        assert [step["iteration"] for step in log["steps"]] == [1, None]
        assert log["iteration_count"] == 1

    def test_a_scenario_with_no_repeated_block_reports_zero_iterations(self, tmp_path):
        """What every step log written before this field existed looks like, so it has to mean
        "no repeated block" rather than "unknown"."""
        logger = ScenarioLogger(["create_part"], tmp_path / "log.json")
        with logger.step("create_part"):
            logger.action()

        log = logger.finalize()

        assert log["iteration_count"] == 0
        assert log["steps"][0]["iteration"] is None

    @pytest.mark.parametrize("iteration", [0, -1])
    def test_iterations_are_one_based(self, tmp_path, iteration):
        """0 would be indistinguishable from "no repeated block" once it reached the report."""
        logger = ScenarioLogger(["create_part"], tmp_path / "log.json")
        with pytest.raises(ScenarioLoggerError, match="1-based"):
            with logger.step("create_part", iteration=iteration):
                pass
