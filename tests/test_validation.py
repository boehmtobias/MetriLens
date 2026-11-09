import copy
import json

import pytest
from pydantic import ValidationError

from metrilens.validation import MetrionExport, StepLog


@pytest.fixture
def step_log_payload(valid_step_log):
    """A step log with `steps` steps, built from the two-step base payload by repeating them."""

    def _build(steps: int = 2) -> dict:
        log = copy.deepcopy(valid_step_log)
        template = log["steps"]
        log["steps"] = [copy.deepcopy(template[index % 2]) for index in range(steps)]
        log["step_count"] = steps
        log["action_count"] = sum(step["action_count"] for step in log["steps"])
        log["entity_count"] = sum(len(step["entities"]) for step in log["steps"])
        return log

    return _build


class TestMetrionExport:
    def test_parses_valid_export(self, tmp_path, write_json, valid_metrion_export):
        export = MetrionExport.from_json_file(
            write_json(tmp_path / "export.json", valid_metrion_export)
        )
        assert export.device_energy_j("CPU") == 12.3
        assert export.summary.average_power_w == 0.55

    def test_missing_device_returns_none(self, tmp_path, write_json, valid_metrion_export):
        export = MetrionExport.from_json_file(
            write_json(tmp_path / "export.json", valid_metrion_export)
        )
        assert export.device_energy_j("DRAM") is None

    def test_rejects_zero_duration(self, tmp_path, write_json, valid_metrion_export):
        valid_metrion_export["metadata"]["duration_seconds"] = 0
        with pytest.raises(ValidationError):
            MetrionExport.from_json_file(write_json(tmp_path / "export.json", valid_metrion_export))

    def test_rejects_end_before_start(self, tmp_path, write_json, valid_metrion_export):
        metadata = valid_metrion_export["metadata"]
        metadata["end_time"], metadata["start_time"] = metadata["start_time"], metadata["end_time"]
        with pytest.raises(ValidationError):
            MetrionExport.from_json_file(write_json(tmp_path / "export.json", valid_metrion_export))

    def test_missing_field_rejected(self, tmp_path, write_json, valid_metrion_export):
        del valid_metrion_export["summary"]["average_power_w"]
        with pytest.raises(ValidationError):
            MetrionExport.from_json_file(write_json(tmp_path / "export.json", valid_metrion_export))


class TestStepLog:
    def test_parses_valid_step_log(self, tmp_path, write_json, valid_step_log):
        step_log = StepLog.from_json_file(write_json(tmp_path / "log.json", valid_step_log))
        assert step_log.step_count == 2
        assert step_log.steps[0].step_id == "create_part"

    def test_rejects_step_count_mismatch(self, tmp_path, write_json, valid_step_log):
        valid_step_log["step_count"] = 5
        with pytest.raises(ValidationError):
            StepLog.from_json_file(write_json(tmp_path / "log.json", valid_step_log))

    def test_rejects_action_count_mismatch(self, tmp_path, write_json, valid_step_log):
        valid_step_log["action_count"] = 99
        with pytest.raises(ValidationError):
            StepLog.from_json_file(write_json(tmp_path / "log.json", valid_step_log))

    def test_rejects_entity_count_mismatch(self, tmp_path, write_json, valid_step_log):
        valid_step_log["entity_count"] = 99
        with pytest.raises(ValidationError):
            StepLog.from_json_file(write_json(tmp_path / "log.json", valid_step_log))

    def test_rejects_end_before_start(self, tmp_path, write_json, valid_step_log):
        valid_step_log["end_time"], valid_step_log["start_time"] = (
            valid_step_log["start_time"],
            valid_step_log["end_time"],
        )
        with pytest.raises(ValidationError):
            StepLog.from_json_file(write_json(tmp_path / "log.json", valid_step_log))


class TestStepLogIterations:
    """The parser has to read both shapes: a step log with a repeated block, and one written
    before iterations existed."""

    def test_it_parses_a_log_without_any_iteration_fields(self, tmp_path, step_log_payload):
        """A step log predating iterations stays readable by the same parser, which is why both
        fields default."""
        log = step_log_payload(steps=2)
        path = tmp_path / "old.json"
        path.write_text(json.dumps(log))

        parsed = StepLog.from_json_file(path)

        assert parsed.iteration_count == 0
        assert all(step.iteration is None for step in parsed.steps)

    def test_iteration_count_must_match_the_steps(self, tmp_path, step_log_payload):
        log = step_log_payload(steps=2)
        log["steps"][0]["iteration"] = 1
        log["steps"][1]["iteration"] = 2
        log["iteration_count"] = 1
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(log))

        with pytest.raises(ValidationError, match="iteration_count"):
            StepLog.from_json_file(path)

    def test_iterations_must_have_no_gaps(self, tmp_path, step_log_payload):
        """A missing middle iteration means a pass was skipped or mislabelled, and a per-iteration
        figure would plot it as if it never ran."""
        log = step_log_payload(steps=2)
        log["steps"][0]["iteration"] = 1
        log["steps"][1]["iteration"] = 3
        log["iteration_count"] = 2
        path = tmp_path / "gap.json"
        path.write_text(json.dumps(log))

        with pytest.raises(ValidationError, match="no gaps"):
            StepLog.from_json_file(path)

    def test_a_well_formed_iterated_log_parses(self, tmp_path, step_log_payload):
        log = step_log_payload(steps=4)
        for index, step in enumerate(log["steps"]):
            step["iteration"] = index // 2 + 1
        log["iteration_count"] = 2
        path = tmp_path / "good.json"
        path.write_text(json.dumps(log))

        parsed = StepLog.from_json_file(path)

        assert parsed.iteration_count == 2
        assert [step.iteration for step in parsed.steps] == [1, 1, 2, 2]
