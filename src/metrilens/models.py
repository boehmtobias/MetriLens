"""SQLAlchemy models for MetriLens's own comparison-layer database."""

import enum
from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, reconstructor, relationship
from transitions import Machine


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RepetitionStatus(enum.Enum):
    RUNNING = "RUNNING"
    OK = "OK"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    FAILED_MEASUREMENT = "FAILED_MEASUREMENT"


class EquivalenceVerdict(enum.Enum):
    PENDING = "PENDING"
    PASS = "PASS"
    FLAGGED = "FLAGGED"
    PASS_OVERRIDDEN = "PASS_OVERRIDDEN"


class _StateMachineMixin:
    """Attaches a `transitions.Machine` keyed directly on a mapped enum column.

    SQLAlchemy calls `__init__` on construction but not when an instance is loaded
    from the database, so the machine is (re)built in both places via `_init_machine`,
    using `reconstructor` for the load path. States are the enum members themselves
    (`transitions` supports this natively), so a triggered transition assigns a real
    enum member back onto the mapped attribute, not a bare string.
    """

    _machine_states: ClassVar[type[enum.Enum]]
    _machine_transitions: ClassVar[list[dict]]
    _machine_attribute: ClassVar[str]
    _machine_default: ClassVar[enum.Enum]

    def _init_machine(self) -> None:
        if getattr(self, self._machine_attribute, None) is None:
            setattr(self, self._machine_attribute, self._machine_default)
        self.machine = Machine(
            model=self,
            states=list(self._machine_states),
            transitions=self._machine_transitions,
            initial=getattr(self, self._machine_attribute),
            model_attribute=self._machine_attribute,
            send_event=False,
        )

    @reconstructor
    def _reconstruct_machine(self) -> None:
        self._init_machine()


class Scenario(Base):
    """The version-independent workload definition. One row per scenario."""

    __tablename__ = "scenario"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str | None]
    steps: Mapped[list] = mapped_column(JSON, default=list)
    # k: how many times the scenario's repeated step block runs inside one repetition. A
    # repetition is one measured execution of the scenario for one app version (one `repetition`
    # row); an iteration is one pass through the repeated block inside it. k belongs to the
    # scenario because a per-repetition result is only interpretable with it and both app
    # versions must run the same k. `server_default` gives existing rows 1, a single pass.
    iterations: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    app_versions: Mapped[list["AppVersion"]] = relationship(back_populates="scenario")
    comparison_sessions: Mapped[list["ComparisonSession"]] = relationship(back_populates="scenario")
    equivalence_checks: Mapped[list["EquivalenceCheck"]] = relationship(
        back_populates="scenario", foreign_keys="EquivalenceCheck.scenario_id"
    )


class AppVersion(Base):
    """One app version realizing a scenario."""

    __tablename__ = "app_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenario.id"), index=True)
    label: Mapped[str] = mapped_column(nullable=False)
    description: Mapped[str | None]
    script_path: Mapped[str] = mapped_column(nullable=False)
    reset_script: Mapped[str] = mapped_column(nullable=False)
    compose_file: Mapped[str] = mapped_column(nullable=False)
    health_url: Mapped[str] = mapped_column(nullable=False)
    version_ref: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    scenario: Mapped["Scenario"] = relationship(back_populates="app_versions")
    version_runs: Mapped[list["VersionRun"]] = relationship(back_populates="app_version")


class ComparisonSession(Base):
    """One comparison session: the shared calibration, execution order and host context.

    Only created once the pre-flight dry run and equivalence gate have already passed;
    a `FLAGGED` pre-flight leaves no row here, just the standalone `equivalence_check` row.
    """

    __tablename__ = "comparison_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenario.id"), index=True)
    host_descriptor: Mapped[str] = mapped_column(nullable=False)
    metrion_config_path: Mapped[str] = mapped_column(nullable=False)
    idle_power_calibration: Mapped[dict] = mapped_column(JSON, nullable=False)
    calibration_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    execution_order: Mapped[list] = mapped_column(JSON, nullable=False)
    random_seed: Mapped[int] = mapped_column(nullable=False)
    # The CPU governor read at session start (`host.describe_cpu_governors`); `None` on a host
    # without `cpufreq`. Read only at start, so it does not show the governor stayed unchanged.
    cpu_governor: Mapped[str | None]
    # Host facts read at session start (`host.collect_host_facts`): CPU, kernel, METRION build
    # and so on. JSON rather than a column per fact, since nothing filters on them and adding a
    # fact then needs no schema change.
    host_facts: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str | None]

    scenario: Mapped["Scenario"] = relationship(back_populates="comparison_sessions")
    version_runs: Mapped[list["VersionRun"]] = relationship(back_populates="comparison_session")


class VersionRun(Base):
    """One app version's full repetition set within a session."""

    __tablename__ = "version_run"
    __table_args__ = (CheckConstraint("repetition_count_planned > 0"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    comparison_session_id: Mapped[int] = mapped_column(
        ForeignKey("comparison_session.id"), index=True
    )
    app_version_id: Mapped[int] = mapped_column(ForeignKey("app_version.id"), index=True)
    repetition_count_planned: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)

    comparison_session: Mapped["ComparisonSession"] = relationship(back_populates="version_runs")
    app_version: Mapped["AppVersion"] = relationship(back_populates="version_runs")
    repetitions: Mapped[list["Repetition"]] = relationship(back_populates="version_run")


class Repetition(_StateMachineMixin, Base):
    """One measurement repetition."""

    __tablename__ = "repetition"
    __table_args__ = (
        CheckConstraint("cpu_energy_j IS NULL OR cpu_energy_j >= 0"),
        CheckConstraint("dram_energy_j IS NULL OR dram_energy_j >= 0"),
        CheckConstraint("runtime_s IS NULL OR runtime_s > 0"),
        CheckConstraint("end_time IS NULL OR start_time IS NULL OR end_time > start_time"),
    )

    _machine_states = RepetitionStatus
    _machine_attribute = "status"
    _machine_default = RepetitionStatus.RUNNING
    _machine_transitions = [
        {"trigger": "complete", "source": RepetitionStatus.RUNNING, "dest": RepetitionStatus.OK},
        {
            "trigger": "fail_validation",
            "source": RepetitionStatus.RUNNING,
            "dest": RepetitionStatus.FAILED_VALIDATION,
        },
        {
            "trigger": "fail_measurement",
            "source": RepetitionStatus.RUNNING,
            "dest": RepetitionStatus.FAILED_MEASUREMENT,
        },
        {
            "trigger": "retry",
            "source": [RepetitionStatus.FAILED_VALIDATION, RepetitionStatus.FAILED_MEASUREMENT],
            "dest": RepetitionStatus.RUNNING,
            "after": "_increment_attempt_count",
        },
    ]

    id: Mapped[int] = mapped_column(primary_key=True)
    version_run_id: Mapped[int] = mapped_column(ForeignKey("version_run.id"), index=True)
    repetition_index: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[RepetitionStatus] = mapped_column(
        SAEnum(RepetitionStatus, native_enum=False),
        default=RepetitionStatus.RUNNING,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(default=0)
    validation_notes: Mapped[str | None]
    measurement_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    start_time: Mapped[datetime | None] = mapped_column(DateTime)
    end_time: Mapped[datetime | None] = mapped_column(DateTime)
    cpu_energy_j: Mapped[float | None]
    dram_energy_j: Mapped[float | None]
    gpu_energy_j: Mapped[float | None]
    runtime_s: Mapped[float | None]
    average_power_w: Mapped[float | None]
    metrion_db_ref: Mapped[str | None]
    raw_export_ref: Mapped[str | None]
    step_log_ref: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    version_run: Mapped["VersionRun"] = relationship(back_populates="repetitions")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        if self.attempt_count is None:
            self.attempt_count = 0
        self._init_machine()

    def _increment_attempt_count(self) -> None:
        self.attempt_count += 1


class EquivalenceCheck(_StateMachineMixin, Base):
    """One pre-flight verdict on whether two app versions' step logs are equivalent.

    Not scoped to a `comparison_session`: every pre-flight dry run produces its own new
    row rather than re-transitioning an old one, so this table is an append-only record.
    """

    __tablename__ = "equivalence_check"

    _machine_states = EquivalenceVerdict
    _machine_attribute = "verdict"
    _machine_default = EquivalenceVerdict.PENDING
    _machine_transitions = [
        {
            "trigger": "pass_check",
            "source": EquivalenceVerdict.PENDING,
            "dest": EquivalenceVerdict.PASS,
        },
        {
            "trigger": "flag",
            "source": EquivalenceVerdict.PENDING,
            "dest": EquivalenceVerdict.FLAGGED,
            "before": "_require_notes",
        },
        {
            "trigger": "override",
            "source": EquivalenceVerdict.FLAGGED,
            "dest": EquivalenceVerdict.PASS_OVERRIDDEN,
            "before": "_require_notes",
        },
    ]

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenario.id"), index=True)
    version_a_id: Mapped[int] = mapped_column(ForeignKey("app_version.id"), index=True)
    version_b_id: Mapped[int] = mapped_column(ForeignKey("app_version.id"), index=True)
    step_count_a: Mapped[int | None]
    step_count_b: Mapped[int | None]
    action_count_a: Mapped[int | None]
    action_count_b: Mapped[int | None]
    entity_count_a: Mapped[int | None]
    entity_count_b: Mapped[int | None]
    verdict: Mapped[EquivalenceVerdict] = mapped_column(
        SAEnum(EquivalenceVerdict, native_enum=False),
        default=EquivalenceVerdict.PENDING,
        index=True,
    )
    notes: Mapped[str | None]
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    scenario: Mapped["Scenario"] = relationship(
        back_populates="equivalence_checks", foreign_keys=[scenario_id]
    )

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._init_machine()

    def _require_notes(self, notes: str | None = None) -> None:
        if not notes:
            raise ValueError("flag/override transitions require a non-empty 'notes' argument")
        self.notes = notes
