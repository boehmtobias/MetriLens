from metrilens.equivalence import check_equivalence
from metrilens.models import EquivalenceVerdict


class TestCheckEquivalence:
    def test_exact_match_passes(self, session, scenario, app_versions, build_step_log):
        version_a, version_b = app_versions
        log_a = build_step_log([("create_part", 4, 1), ("adjust_stock", 3, 1)])
        log_b = build_step_log([("create_part", 6, 1), ("adjust_stock", 2, 1)])

        check = check_equivalence(session, scenario.id, version_a.id, version_b.id, log_a, log_b)

        assert check.verdict is EquivalenceVerdict.PASS
        assert check.notes is None
        assert check.step_count_a == 2
        assert check.action_count_a == 7
        assert check.action_count_b == 8

    def test_action_count_difference_alone_does_not_flag(
        self, session, scenario, app_versions, build_step_log
    ):
        version_a, version_b = app_versions
        log_a = build_step_log([("create_part", 1, 1)])
        log_b = build_step_log([("create_part", 99, 1)])

        check = check_equivalence(session, scenario.id, version_a.id, version_b.id, log_a, log_b)

        assert check.verdict is EquivalenceVerdict.PASS

    def test_step_count_mismatch_flags(self, session, scenario, app_versions, build_step_log):
        version_a, version_b = app_versions
        log_a = build_step_log([("create_part", 1, 1), ("adjust_stock", 1, 0)])
        log_b = build_step_log([("create_part", 1, 1)])

        check = check_equivalence(session, scenario.id, version_a.id, version_b.id, log_a, log_b)

        assert check.verdict is EquivalenceVerdict.FLAGGED
        assert "step_count 2 != 1" in check.notes
        assert "entity_count" not in check.notes
        assert "first diverges at step 'create_part'" in check.notes

    def test_entity_count_mismatch_flags_with_diverging_step(
        self, session, scenario, app_versions, build_step_log
    ):
        version_a, version_b = app_versions
        log_a = build_step_log([("create_part", 1, 1), ("adjust_stock", 1, 2)])
        log_b = build_step_log([("create_part", 1, 1), ("adjust_stock", 1, 1)])

        check = check_equivalence(session, scenario.id, version_a.id, version_b.id, log_a, log_b)

        assert check.verdict is EquivalenceVerdict.FLAGGED
        assert "entity_count 3 != 2" in check.notes
        assert "adjust_stock" in check.notes

    def test_row_is_persisted_and_checked_at_set(
        self, session, scenario, app_versions, build_step_log
    ):
        version_a, version_b = app_versions
        log_a = build_step_log([("create_part", 1, 1)])
        log_b = build_step_log([("create_part", 1, 1)])

        check = check_equivalence(session, scenario.id, version_a.id, version_b.id, log_a, log_b)

        assert check.id is not None
        assert check.checked_at is not None

    def test_each_call_creates_a_fresh_row(self, session, scenario, app_versions, build_step_log):
        version_a, version_b = app_versions
        log_a = build_step_log([("create_part", 1, 1)])
        log_b = build_step_log([("create_part", 1, 1)])

        first = check_equivalence(session, scenario.id, version_a.id, version_b.id, log_a, log_b)
        second = check_equivalence(session, scenario.id, version_a.id, version_b.id, log_a, log_b)

        assert first.id != second.id
