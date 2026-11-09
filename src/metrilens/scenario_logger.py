"""Step logger for the per-version Playwright scripts.

Used as `with logger.step("create_part"): ...` around each logical step. The script never
decides where its step log is written; the orchestrator supplies `output_path` when it
invokes the script, so the script does not know which repetition it is running as.

Every timestamp here is timezone-aware UTC (`datetime.now(UTC)`), matching the rest
of the pipeline; `writer.py` copies these values straight onto
`repetition.start_time`/`end_time`.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from metrilens import recording


class ScenarioLoggerError(RuntimeError):
    """Raised on a script-author error: an undeclared/nested step, or a call outside one."""


@dataclass
class _StepRecord:
    step_id: str
    start_time: datetime
    end_time: datetime | None = None
    action_count: int = 0
    entities: list[dict] = field(default_factory=list)
    # Which pass through the scenario's repeated step block this record belongs to, or None
    # for a step outside that block. See `ScenarioLogger.step`.
    iteration: int | None = None


class ScenarioLogger:
    """Records step/action/entity counts for one Playwright script run."""

    def __init__(self, steps: list[str], output_path: Path) -> None:
        self._declared_steps = set(steps)
        self._output_path = output_path
        self._records: list[_StepRecord] = []
        self._current: _StepRecord | None = None

    @contextmanager
    def step(self, step_id: str, iteration: int | None = None) -> Iterator["ScenarioLogger"]:
        """Time one logical step, validating it against the scenario's declared steps.

        A version-specific UI wrinkle needed to accomplish this step (an extra
        confirmation dialog, an extra click) belongs inside this block as an action,
        never as its own step.

        Args:
            step_id: Must be declared in the scenario's `steps` list.
            iteration: Which pass through the scenario's repeated step block this step
                belongs to, 1-based, or `None` for a step outside that block. Not to be
                confused with a repetition: a repetition is one measured execution of
                the whole scenario, an iteration is one pass inside it, and a step id
                may appear several times in one log, once per iteration, which is why
                the declared step list is a vocabulary rather than an expected sequence.
        """
        if iteration is not None and iteration < 1:
            raise ScenarioLoggerError(f"iteration must be 1-based, got {iteration}")
        if step_id not in self._declared_steps:
            raise ScenarioLoggerError(
                f"step '{step_id}' is not declared in this scenario's steps: "
                f"{sorted(self._declared_steps)}"
            )
        if self._current is not None:
            raise ScenarioLoggerError(
                f"step '{step_id}' opened while step '{self._current.step_id}' is still open"
            )
        record = _StepRecord(step_id=step_id, start_time=datetime.now(UTC), iteration=iteration)
        self._current = record
        # Presentation only, and a no-op in every measured repetition: under `metrilens record`
        # this is what puts the step's name into the video's caption bar, timed to the same
        # boundary this log records rather than to a timestamp matched up afterwards.
        recording.caption(step_id, iteration)
        try:
            yield self
        finally:
            record.end_time = datetime.now(UTC)
            self._records.append(record)
            self._current = None

    def action(self) -> None:
        """Count one UI action within the currently open step."""
        if self._current is None:
            raise ScenarioLoggerError("action() called outside an active step() block")
        self._current.action_count += 1

    def entity_created(self, entity_type: str, identifier: str | None = None) -> None:
        """Record one domain entity created within the currently open step.

        Explicit rather than inferred from clicks, since which UI actions create an
        entity is domain knowledge this helper can't derive on its own.
        """
        if self._current is None:
            raise ScenarioLoggerError("entity_created() called outside an active step() block")
        self._current.entities.append({"entity_type": entity_type, "identifier": identifier})

    def finalize(self) -> dict:
        """Write the ordered step log to `output_path`; also returns it as a dict.

        The log's own `start_time`/`end_time` (first step's start to last step's end)
        is the declared-step span, in UTC; this is what the orchestrator narrows
        `metrion analyze`'s window to, so process startup and login stay outside the
        measured span even though `metrion monitor` itself runs around the whole
        process.
        """
        if not self._records:
            raise ScenarioLoggerError("finalize() called with no steps logged")

        step_log = {
            "start_time": self._records[0].start_time.isoformat(),
            "end_time": self._records[-1].end_time.isoformat(),
            "step_count": len(self._records),
            "action_count": sum(record.action_count for record in self._records),
            "entity_count": sum(len(record.entities) for record in self._records),
            # k as it actually ran, against `scenario.iterations` as it was declared. Reporting
            # both is the point: a script and its definition drifting apart is then a visible
            # mismatch rather than something the averages absorb. 0 for a scenario with no
            # repeated block.
            "iteration_count": len(
                {record.iteration for record in self._records if record.iteration is not None}
            ),
            "steps": [
                {
                    "step_id": record.step_id,
                    "iteration": record.iteration,
                    "start_time": record.start_time.isoformat(),
                    "end_time": record.end_time.isoformat(),
                    "action_count": record.action_count,
                    "entities": record.entities,
                }
                for record in self._records
            ],
        }
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._output_path.write_text(json.dumps(step_log, indent=2))
        return step_log
