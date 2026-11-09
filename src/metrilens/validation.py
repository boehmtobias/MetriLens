"""Pydantic models for METRION's export and the step log.

Every insert into a `repetition` row is validated before it's attempted: METRION's
exported `energy_stats_*.json` (from `metrion analyze --export-summary`) and the
Playwright step log (`ScenarioLogger.finalize()`'s output) are parsed into these
models first. A failure here is what drives a `repetition` row to
`FAILED_VALIDATION` rather than `OK` (writer.py).
"""

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, model_validator


class DeviceTotals(BaseModel):
    """One device's (`CPU`/`DRAM`/`GPU`) energy totals from METRION's export."""

    total: float
    idle: float
    dynamic: float


class MetrionExportMetadata(BaseModel):
    filter: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    unit: str

    @model_validator(mode="after")
    def _duration_positive(self) -> "MetrionExportMetadata":
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be > 0")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class MetrionExportSummary(BaseModel):
    total_energy: float
    idle_energy: float
    dynamic_energy: float
    average_power_w: float


class MetrionExport(BaseModel):
    """`energy_stats_<timestamp>.json`, METRION's `--export-summary` statistics file."""

    metadata: MetrionExportMetadata
    device_totals: dict[str, DeviceTotals]
    summary: MetrionExportSummary

    @classmethod
    def from_json_file(cls, path: Path) -> "MetrionExport":
        return cls.model_validate(json.loads(Path(path).read_text()))

    def device_energy_j(self, device: str) -> float | None:
        """Energy total for `device` (e.g. `"CPU"`), or `None` if METRION didn't report it.

        DRAM in particular is absent on hardware with no DRAM RAPL domain;
        `None` here is the honest signal, not `0`.
        """
        totals = self.device_totals.get(device.upper())
        return totals.total if totals is not None else None


class StepLogEntity(BaseModel):
    entity_type: str
    identifier: str | None = None


class StepLogStep(BaseModel):
    step_id: str
    start_time: datetime
    end_time: datetime
    action_count: int
    entities: list[StepLogEntity]
    # None for a step outside the repeated block. Optional, so a step log without
    # iterations still parses.
    iteration: int | None = None


class StepLog(BaseModel):
    """`ScenarioLogger.finalize()`'s output, one Playwright script run."""

    start_time: datetime
    end_time: datetime
    step_count: int
    action_count: int
    entity_count: int
    iteration_count: int = 0
    steps: list[StepLogStep]

    @model_validator(mode="after")
    def _counts_match_steps(self) -> "StepLog":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        if self.step_count != len(self.steps):
            raise ValueError(
                f"step_count ({self.step_count}) does not match len(steps) ({len(self.steps)})"
            )
        if self.action_count != sum(step.action_count for step in self.steps):
            raise ValueError("action_count does not match the sum of each step's action_count")
        if self.entity_count != sum(len(step.entities) for step in self.steps):
            raise ValueError("entity_count does not match the sum of each step's entities")
        iterations = {step.iteration for step in self.steps if step.iteration is not None}
        if self.iteration_count != len(iterations):
            raise ValueError(
                f"iteration_count ({self.iteration_count}) does not match the "
                f"{len(iterations)} distinct iteration(s) its steps carry"
            )
        if iterations and sorted(iterations) != list(range(1, len(iterations) + 1)):
            raise ValueError(f"iterations must be 1..k with no gaps, got {sorted(iterations)}")
        return self

    @classmethod
    def from_json_file(cls, path: Path) -> "StepLog":
        return cls.model_validate(json.loads(Path(path).read_text()))
