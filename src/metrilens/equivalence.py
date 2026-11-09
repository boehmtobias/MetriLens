"""Compares two versions' step logs for a scenario and produces the equivalence verdict.

Computed from the pre-flight dry run before any real measurement begins. Different
counts warrant different strictness: `step_count`/`entity_count` are enforced exactly
(the scenario is the fixed, version-independent definition; the functional outcome
should be identical regardless of UI); `action_count` is recorded but never enforced,
since two UIs can legitimately need a different number of clicks for the same logical
step, a real cost difference, not measurement noise to hide.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from metrilens.models import EquivalenceCheck
from metrilens.validation import StepLog, StepLogStep


def _first_divergence(steps_a: list[StepLogStep], steps_b: list[StepLogStep]) -> str | None:
    """The step id where the two logs first disagree (id or entity count).

    `None` if every step in the shorter log matches its counterpart in the
    longer one (the divergence is then purely the length difference itself).
    """
    for step_a, step_b in zip(steps_a, steps_b, strict=False):
        if step_a.step_id != step_b.step_id or len(step_a.entities) != len(step_b.entities):
            return step_a.step_id
    shorter = steps_a if len(steps_a) <= len(steps_b) else steps_b
    return shorter[-1].step_id if shorter and len(steps_a) != len(steps_b) else None


def check_equivalence(
    session: Session,
    scenario_id: int,
    version_a_id: int,
    version_b_id: int,
    step_log_a: StepLog,
    step_log_b: StepLog,
) -> EquivalenceCheck:
    """Produce a fresh `equivalence_check` row for one pre-flight dry run.

    Always a new `PENDING` row, never a re-transitioned prior one for the same
    scenario/version pair: every dry run also serves as the pipeline's mechanical
    smoke test, so its outcome is recorded regardless of past verdicts.
    """
    check = EquivalenceCheck(
        scenario_id=scenario_id,
        version_a_id=version_a_id,
        version_b_id=version_b_id,
        step_count_a=step_log_a.step_count,
        step_count_b=step_log_b.step_count,
        action_count_a=step_log_a.action_count,
        action_count_b=step_log_b.action_count,
        entity_count_a=step_log_a.entity_count,
        entity_count_b=step_log_b.entity_count,
        checked_at=datetime.now(UTC),
    )
    session.add(check)
    session.flush()

    mismatches = []
    if step_log_a.step_count != step_log_b.step_count:
        mismatches.append(f"step_count {step_log_a.step_count} != {step_log_b.step_count}")
    if step_log_a.entity_count != step_log_b.entity_count:
        mismatches.append(f"entity_count {step_log_a.entity_count} != {step_log_b.entity_count}")

    if mismatches:
        divergence = _first_divergence(step_log_a.steps, step_log_b.steps)
        notes = "; ".join(mismatches)
        if divergence is not None:
            notes += f"; first diverges at step '{divergence}'"
        check.flag(notes=notes)
    else:
        check.pass_check()

    session.add(check)
    session.commit()
    return check
