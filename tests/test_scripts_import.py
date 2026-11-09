"""Every script under `scripts/` must at least import.

A renamed or moved dependency in the package otherwise stays invisible until someone runs the
script by hand. Behaviour belongs in each script's own test module.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _script_paths() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS_DIR.glob("*.py")
        if not path.name.startswith("_") and path.name != "__init__.py"
    )


def test_there_are_scripts_to_check():
    """So a glob that silently matches nothing cannot make this whole module vacuous."""
    assert _script_paths()


@pytest.mark.parametrize("script_path", _script_paths(), ids=lambda path: path.stem)
def test_script_imports(script_path, monkeypatch):
    """Importing must not raise. Every script guards execution behind `__main__`.

    `scripts/` goes on `sys.path` because `check_run_session_sudo` imports a sibling by name,
    which is how it runs for real. The module is registered in `sys.modules` before it is
    executed, and not merely built from its spec: a script defining a dataclass with a
    string-form annotation makes `dataclasses` look its own module up by name, which returns
    `None` for an unregistered module and fails inside the stdlib rather than in the script.
    """
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(script_path.stem, script_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
