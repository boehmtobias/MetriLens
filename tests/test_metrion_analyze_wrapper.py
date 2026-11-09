"""The root-owned analyze wrapper's own validation is the safety property.

The sudoers grant is `<script> *`, so every argument is attacker-chosen from the
measurement account's point of view and nothing but this script constrains it. These
tests are therefore the security boundary, not just correctness checks.
"""

import metrion_analyze_wrapper as wrapper
import pytest

from metrilens.metrion_integration import EXPECTED_WRAPPER_ANALYZE_ARGS

_START = "2026-01-01 00:00:00"
_END = "2026-01-01 00:00:01"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A valid database inside a stand-in DB_ROOT."""
    root = tmp_path / "metrion-data"
    root.mkdir()
    path = root / "monitor_2026_01_01_00_00_00.db"
    path.write_bytes(b"duckdb")
    monkeypatch.setattr(wrapper, "DB_ROOT", root)
    return path


class TestFixedArgvDoesNotDrift:
    def test_wrapper_argv_matches_what_the_package_declares(self):
        # A changed filter flag would silently stop matching the sudoers grant. The wrapper is
        # stdlib-only so it cannot import this, hence the assertion.
        assert wrapper.FIXED_ANALYZE_ARGS == EXPECTED_WRAPPER_ANALYZE_ARGS

    def test_the_export_stays_unfiltered(self):
        # attribution.py's numbers depend on this specific value.
        args = wrapper.FIXED_ANALYZE_ARGS
        assert args[args.index("--filter-cmdline") + 1] == "."


class TestAccepts:
    def test_valid_arguments_build_the_full_argv(self, db):
        argv = wrapper.build_argv(str(db), _START, _END)
        assert argv[0] == str(wrapper.METRION_BINARY)
        assert argv[1] == "analyze"
        assert argv[argv.index("--db-path") + 1] == str(db.resolve())
        assert argv[argv.index("--start-time") + 1] == _START
        assert argv[argv.index("--end-time") + 1] == _END
        assert tuple(argv[-len(wrapper.FIXED_ANALYZE_ARGS) :]) == wrapper.FIXED_ANALYZE_ARGS

    def test_the_binary_is_never_taken_from_the_arguments(self, db):
        # The load-bearing property: a binary path from argv would make the grant an
        # arbitrary-root-exec primitive, worse than the wildcard it replaces.
        argv = wrapper.build_argv(str(db), _START, _END)
        assert argv[0] == str(wrapper.METRION_BINARY)
        assert str(wrapper.METRION_BINARY).startswith("/opt/metrion-runtime/")


class TestRejects:
    def test_a_path_outside_the_db_root(self, db, tmp_path):
        outside = tmp_path / "elsewhere.db"
        outside.write_bytes(b"x")
        with pytest.raises(wrapper.ValidationError, match="not directly inside"):
            wrapper.build_argv(str(outside), _START, _END)

    def test_traversal_out_of_the_db_root(self, db):
        escape = db.parent / ".." / "etc" / "shadow"
        with pytest.raises(wrapper.ValidationError, match="not directly inside"):
            wrapper.build_argv(str(escape), _START, _END)

    def test_a_symlink_even_when_it_points_somewhere_valid(self, db):
        link = db.parent / "link.db"
        link.symlink_to(db)
        with pytest.raises(wrapper.ValidationError, match="symlink"):
            wrapper.build_argv(str(link), _START, _END)

    def test_a_subdirectory_of_the_db_root(self, db):
        nested = db.parent / "nested"
        nested.mkdir()
        inner = nested / "monitor.db"
        inner.write_bytes(b"x")
        with pytest.raises(wrapper.ValidationError, match="not directly inside"):
            wrapper.build_argv(str(inner), _START, _END)

    def test_a_directory_rather_than_a_file(self, db):
        with pytest.raises(wrapper.ValidationError, match="not directly inside|not a regular file"):
            wrapper.build_argv(str(db.parent), _START, _END)

    def test_a_missing_file(self, db):
        with pytest.raises(wrapper.ValidationError, match="not a regular file"):
            wrapper.build_argv(str(db.parent / "absent.db"), _START, _END)

    @pytest.mark.parametrize(
        "bad",
        [
            "2026-01-01",
            "2026-01-01T00:00:00",
            "2026-1-1 0:0:0",
            "2026-01-01 00:00:00 --export-summary",
            "; rm -rf /",
            "",
            "2026-01-01 00:00:00\n2026-01-01 00:00:01",
        ],
    )
    def test_a_malformed_start_time(self, db, bad):
        with pytest.raises(wrapper.ValidationError, match="start time"):
            wrapper.build_argv(str(db), bad, _END)

    def test_a_malformed_end_time(self, db):
        with pytest.raises(wrapper.ValidationError, match="end time"):
            wrapper.build_argv(str(db), _START, "not-a-timestamp")


class TestMain:
    def test_wrong_argument_count_exits_nonzero(self, capsys):
        assert wrapper.main([]) == 2
        assert wrapper.main(["only-one"]) == 2
        assert wrapper.main(["a", "b", "c", "d"]) == 2
        assert "expected exactly 3 arguments" in capsys.readouterr().err

    def test_a_rejected_argument_exits_nonzero_and_explains(self, db, capsys):
        assert wrapper.main([str(db), "nope", _END]) == 2
        err = capsys.readouterr().err
        assert "refusing to run" in err and "start time" in err

    def test_a_missing_metrion_binary_exits_nonzero(self, db, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(wrapper, "METRION_BINARY", tmp_path / "absent-metrion")
        assert wrapper.main([str(db), _START, _END]) == 2
        assert "METRION binary not found" in capsys.readouterr().err

    def test_valid_arguments_reach_execv_with_the_built_argv(self, db, monkeypatch, tmp_path):
        fake_binary = tmp_path / "metrion"
        fake_binary.write_text("#!/bin/sh\n")
        monkeypatch.setattr(wrapper, "METRION_BINARY", fake_binary)
        seen = {}

        def fake_execv(path, argv):
            seen["path"], seen["argv"] = path, argv

        monkeypatch.setattr(wrapper.os, "execv", fake_execv)
        wrapper.main([str(db), _START, _END])
        assert seen["path"] == str(fake_binary)
        assert seen["argv"] == wrapper.build_argv(str(db), _START, _END)
