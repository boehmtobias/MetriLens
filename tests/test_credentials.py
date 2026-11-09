import os

import pytest

from metrilens.credentials import (
    CredentialError,
    find_env_file,
    load_env_file,
    parse_env_file,
    require_env,
)


@pytest.fixture(autouse=True)
def _no_env_file_override(monkeypatch):
    """Keep every test off the developer's real `.env`, wherever it sits.

    `find_env_file` searches upward from the working directory, which under pytest
    is the repository root, so without this a local `.env` would leak into the
    tests and make them pass or fail depending on the machine.
    """
    monkeypatch.delenv("METRILENS_ENV_FILE", raising=False)
    monkeypatch.setenv("METRILENS_ENV_FILE", "/nonexistent/.env")


class TestParseEnvFile:
    def test_reads_simple_pairs(self):
        assert parse_env_file("A=1\nB=two\n") == {"A": "1", "B": "two"}

    def test_ignores_blank_lines_and_comments(self):
        text = "\n# a comment\n\nA=1\n   # indented comment\n"
        assert parse_env_file(text) == {"A": "1"}

    def test_strips_an_export_prefix(self):
        assert parse_env_file("export A=1\n") == {"A": "1"}

    def test_strips_matching_quotes_but_keeps_inner_spaces(self):
        assert parse_env_file("A=\" pw \"\nB='x'\n") == {"A": " pw ", "B": "x"}

    def test_keeps_a_hash_inside_a_value(self):
        # A trailing `#` is not a comment marker: passwords contain them.
        assert parse_env_file("A=pw#1\n") == {"A": "pw#1"}

    def test_keeps_an_equals_sign_inside_a_value(self):
        assert parse_env_file("A=a=b\n") == {"A": "a=b"}

    def test_ignores_lines_without_an_assignment(self):
        assert parse_env_file("nonsense\nA=1\n") == {"A": "1"}


class TestLoadEnvFile:
    def test_sets_variables_from_the_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("METRILENS_TEST_A=1\n")
        monkeypatch.delenv("METRILENS_TEST_A", raising=False)
        assert load_env_file(env_file) == ["METRILENS_TEST_A"]
        assert os.environ["METRILENS_TEST_A"] == "1"

    def test_never_overrides_an_already_set_variable(self, tmp_path, monkeypatch):
        # The load-bearing rule: the real environment is the more specific source,
        # so a stale checked-out `.env` must not win over an explicit override.
        env_file = tmp_path / ".env"
        env_file.write_text("METRILENS_TEST_B=from_file\n")
        monkeypatch.setenv("METRILENS_TEST_B", "from_environment")
        assert load_env_file(env_file) == []
        assert os.environ["METRILENS_TEST_B"] == "from_environment"

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert load_env_file(tmp_path / "absent") == []


class TestFindEnvFile:
    def test_the_override_variable_wins(self, tmp_path, monkeypatch):
        env_file = tmp_path / "elsewhere.env"
        env_file.write_text("A=1\n")
        monkeypatch.setenv("METRILENS_ENV_FILE", str(env_file))
        assert find_env_file() == env_file

    def test_an_override_pointing_nowhere_finds_nothing(self, monkeypatch):
        monkeypatch.setenv("METRILENS_ENV_FILE", "/nonexistent/.env")
        assert find_env_file() is None

    def test_searches_upward_from_the_given_directory(self, tmp_path, monkeypatch):
        monkeypatch.delenv("METRILENS_ENV_FILE", raising=False)
        (tmp_path / ".env").write_text("A=1\n")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_env_file(nested) == tmp_path / ".env"


class TestRequireEnv:
    def test_returns_a_set_value(self, monkeypatch):
        monkeypatch.setenv("METRILENS_TEST_C", "value")
        assert require_env("METRILENS_TEST_C") == "value"

    def test_falls_back_to_the_env_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("METRILENS_TEST_D=from_file\n")
        monkeypatch.setenv("METRILENS_ENV_FILE", str(env_file))
        monkeypatch.delenv("METRILENS_TEST_D", raising=False)
        assert require_env("METRILENS_TEST_D") == "from_file"

    def test_raises_naming_the_variable_when_unset(self, monkeypatch):
        monkeypatch.delenv("METRILENS_TEST_E", raising=False)
        with pytest.raises(CredentialError, match="METRILENS_TEST_E"):
            require_env("METRILENS_TEST_E")

    def test_treats_an_empty_value_as_unset(self, monkeypatch):
        # An empty password would otherwise reach the app as a rejected login and
        # resurface much later as a login timeout, looking like a slow app.
        monkeypatch.setenv("METRILENS_TEST_F", "")
        with pytest.raises(CredentialError, match="METRILENS_TEST_F"):
            require_env("METRILENS_TEST_F")

    def test_the_error_does_not_contain_a_value(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("METRILENS_TEST_OTHER=secret\n")
        monkeypatch.setenv("METRILENS_ENV_FILE", str(env_file))
        monkeypatch.delenv("METRILENS_TEST_G", raising=False)
        with pytest.raises(CredentialError) as excinfo:
            require_env("METRILENS_TEST_G")
        assert "secret" not in str(excinfo.value)
