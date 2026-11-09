import csv
import json
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import playwright_support
import pytest
from click.testing import CliRunner
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from metrilens.models import AppVersion, Base, ComparisonSession, Scenario, VersionRun
from metrilens.validation import StepLog


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def runner():
    """A `CliRunner` for invoking a `click` command in-process."""
    return CliRunner()


def build_step_log_from_tuples(steps: list[tuple[str, int, int]]) -> StepLog:
    """Build a valid `StepLog` from `(step_id, action_count, entity_count)` tuples.

    A plain function rather than a fixture-only helper, since a couple of test files also need
    it from inside their own non-fixture helper methods; `build_step_log` below is the
    fixture-DI form of the same builder for tests that just want it as a parameter.
    """
    now = datetime.now(UTC)
    step_dicts = []
    for i, (step_id, action_count, entity_count) in enumerate(steps):
        step_dicts.append(
            {
                "step_id": step_id,
                "start_time": (now + timedelta(seconds=i)).isoformat(),
                "end_time": (now + timedelta(seconds=i + 1)).isoformat(),
                "action_count": action_count,
                "entities": [
                    {"entity_type": "thing", "identifier": str(j)} for j in range(entity_count)
                ],
            }
        )
    return StepLog(
        start_time=step_dicts[0]["start_time"],
        end_time=step_dicts[-1]["end_time"],
        step_count=len(step_dicts),
        action_count=sum(s[1] for s in steps),
        entity_count=sum(s[2] for s in steps),
        steps=step_dicts,
    )


@pytest.fixture
def build_step_log():
    """Return a function building a valid `StepLog` from `(step_id, action_count,
    entity_count)` tuples, for tests that need a parsed step log rather than a JSON payload."""
    return build_step_log_from_tuples


def completed_process(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    """A `subprocess.CompletedProcess` double for monkeypatching `subprocess.run`."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def write_json():
    """Write `data` as JSON to `path` and return `path`, for building on-disk fixture files."""

    def _write(path, data):
        path.write_text(json.dumps(data))
        return path

    return _write


@pytest.fixture
def valid_metrion_export():
    """A METRION export payload that satisfies `MetrionExport`'s validation as-is."""
    return {
        "metadata": {
            "filter": "inventree",
            "start_time": "2026-07-28T15:05:29.954640",
            "end_time": "2026-07-28T15:05:52.471005",
            "duration_seconds": 22.5,
            "unit": "J",
        },
        "device_totals": {
            "CPU": {"total": 12.3, "idle": 2.1, "dynamic": 10.2},
        },
        "summary": {
            "total_energy": 12.3,
            "idle_energy": 2.1,
            "dynamic_energy": 10.2,
            "average_power_w": 0.55,
        },
    }


@pytest.fixture
def valid_step_log():
    """A step log payload that satisfies `StepLog`'s validation as-is: two steps, no iterations."""
    return {
        "start_time": "2026-07-28T15:05:29.954640",
        "end_time": "2026-07-28T15:05:52.471005",
        "step_count": 2,
        "action_count": 3,
        "entity_count": 1,
        "steps": [
            {
                "step_id": "create_part",
                "start_time": "2026-07-28T15:05:29.954640",
                "end_time": "2026-07-28T15:05:33.691076",
                "action_count": 2,
                "entities": [{"entity_type": "part", "identifier": "p1"}],
            },
            {
                "step_id": "adjust_stock",
                "start_time": "2026-07-28T15:05:33.691082",
                "end_time": "2026-07-28T15:05:52.471005",
                "action_count": 1,
                "entities": [],
            },
        ],
    }


@pytest.fixture
def scenario(session):
    scenario = Scenario(name="Test Scenario", steps=["create_part", "adjust_stock"])
    session.add(scenario)
    session.flush()
    return scenario


@pytest.fixture
def app_versions(session, scenario):
    version_a = AppVersion(
        scenario_id=scenario.id,
        label="DEBUG=True",
        script_path="a.py",
        reset_script="reset_a.sh",
        compose_file="a/docker-compose.yml",
        health_url="http://localhost:8280/api/",
    )
    version_b = AppVersion(
        scenario_id=scenario.id,
        label="DEBUG=False",
        script_path="b.py",
        reset_script="reset_b.sh",
        compose_file="b/docker-compose.yml",
        health_url="http://localhost:8180/api/",
    )
    session.add_all([version_a, version_b])
    session.flush()
    return version_a, version_b


@pytest.fixture
def version_run(session, scenario, app_versions):
    version_a, _ = app_versions
    comparison_session = ComparisonSession(
        scenario_id=scenario.id,
        host_descriptor="workstation1",
        metrion_config_path="configs/config.yaml",
        idle_power_calibration={"package": 5.1},
        calibration_timestamp=datetime.now(UTC),
        execution_order=[version_a.id],
        random_seed=42,
    )
    session.add(comparison_session)
    session.flush()
    run = VersionRun(
        comparison_session_id=comparison_session.id,
        app_version_id=version_a.id,
        repetition_count_planned=2,
    )
    session.add(run)
    session.flush()
    return run


# Playwright doubles for the shared `playwright_support` module. They live here rather than in
# one test file because two files need them: `test_playwright_support.py` tests the helper
# itself, `test_session_flow_definitions.py` checks that the version scripts wire it up. One set
# of doubles for the same reason there is one shared login implementation.


class FakeLocator:
    """One locator, recording what was typed into it and how the form was submitted.

    `name` doubles as the locator's identity in assertions, so a test can say which element a
    condition wait was pointed at without a real DOM.
    """

    def __init__(self, page: "FakePage", name: str):
        self.page = page
        self.name = name

    def fill(self, value):
        self.page.filled[self.name] = value

    def press(self, key):
        self.page.submits.append(f"press:{key}")

    def click(self, timeout=None):
        self.page.submits.append(f"click:{timeout}")
        self.page.interactions.append(f"click:{self.name}")


class FakeKeyboard:
    def __init__(self, page: "FakePage"):
        self._page = page

    def type(self, text):
        self._page.interactions.append(f"type:{text}")

    def press(self, key):
        self._page.interactions.append(f"press:{key}")


class FakeResponse:
    """The slice of Playwright's `Response` that `_response_matcher` inspects."""

    def __init__(self, url: str, method: str = "GET", status: int = 200):
        self.url = url
        self.status = status
        self.request = SimpleNamespace(method=method)


class FakePage:
    """The narrow slice of Playwright's `Page` that `login` actually touches.

    Enough to exercise the retry budget without a browser: `wait_for_url` fails for the first
    `failing_rounds` calls and records the timeout it was given each time.
    """

    def __init__(self, failing_rounds: int = 0):
        self._failing_rounds = failing_rounds
        # Only a recorded page has a video; an unrecorded one is what `new_page` hands back.
        self.video: FakeVideo | None = None
        self.closed = False
        self.evaluated: list[tuple[str, object]] = []
        self.visited: list[str] = []
        self.filled: dict[str, str] = {}
        self.redirect_timeouts: list[int] = []
        self.submits: list[str] = []
        # Ordered log of everything the condition waits do, which is what their tests assert on:
        # a wait that fires in the wrong order is the failure mode that matters here.
        self.interactions: list[str] = []
        self.awaited_responses: list[tuple] = []
        self.keyboard = FakeKeyboard(self)

    def goto(self, url):
        self.visited.append(url)

    def evaluate(self, expression, arg=None):
        self.evaluated.append((expression, arg))

    def close(self):
        self.closed = True

    def get_by_placeholder(self, name):
        return FakeLocator(self, name)

    def get_by_role(self, role, name=None):
        return FakeLocator(self, f"{role}:{name}")

    def get_by_label(self, name):
        return FakeLocator(self, name)

    def wait_for_url(self, pattern, timeout=None):
        self.redirect_timeouts.append(timeout)
        if len(self.redirect_timeouts) <= self._failing_rounds:
            raise PlaywrightTimeoutError("no redirect")

    @contextmanager
    def expect_response(self, predicate, timeout=None):
        """Record the predicate and the point in the sequence the wait was opened and closed."""
        self.awaited_responses.append((predicate, timeout))
        index = len(self.awaited_responses) - 1
        self.interactions.append(f"await_open:{index}")
        yield
        self.interactions.append(f"await_close:{index}")


class FakeVideo:
    """Playwright's `Video` handle, which is the only thing that names the saved file."""

    def __init__(self):
        self.saved_to: str | None = None
        self.deleted = False

    def save_as(self, path):
        self.saved_to = str(path)

    def delete(self):
        self.deleted = True


class FakeContext:
    """A browser context, which is what video recording actually attaches to.

    Records the launch arguments and the init scripts, since those are the whole of what
    `recording.recorded_page` configures, and the close order, since that is what decides
    whether a playable file exists at the end.
    """

    def __init__(self, browser: "FakeBrowser", **kwargs):
        self.browser = browser
        self.kwargs = kwargs
        self.init_scripts: list[str] = []
        self.closed = False

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def new_page(self):
        page = FakePage()
        page.video = FakeVideo()
        self.browser.pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.closed = False
        self.pages: list[FakePage] = []
        self.contexts: list[FakeContext] = []

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def new_context(self, **kwargs):
        context = FakeContext(self, **kwargs)
        self.contexts.append(context)
        return context

    def close(self):
        self.closed = True


@pytest.fixture
def fake_page():
    """The `FakePage` class itself, so a test can pick its own `failing_rounds`."""
    return FakePage


@pytest.fixture
def fake_response():
    """The `FakeResponse` class, for exercising a condition wait's predicate directly."""
    return FakeResponse


@pytest.fixture
def fake_browser():
    return FakeBrowser()


@pytest.fixture
def patch_playwright(monkeypatch):
    """Replace `playwright_support.sync_playwright` with one handing out `browser`.

    Returns the list recording the keyword arguments Chromium is launched with, which is the
    only thing `browser_page` passes through and therefore the only thing worth asserting on.
    """

    def _patch(browser):
        launched: list[dict] = []

        def _launch(**kwargs):
            launched.append(kwargs)
            return browser

        class _FakePlaywright:
            def __init__(self):
                self.chromium = SimpleNamespace(launch=_launch)

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        monkeypatch.setattr(playwright_support, "sync_playwright", _FakePlaywright)
        return launched

    return _patch


@pytest.fixture
def target_app_credentials(monkeypatch, tmp_path):
    """Set the variables `login` resolves, and cut off the `.env` fallback.

    The second half matters on the measurement host, where a real `.env` exists: without it a
    test asserting that a missing variable fails would instead find the host's own password.
    """
    monkeypatch.setenv(playwright_support.ADMIN_USERNAME_ENV, "admin")
    monkeypatch.setenv(playwright_support.ADMIN_PASSWORD_ENV, "secret")
    monkeypatch.setenv("METRILENS_ENV_FILE", str(tmp_path / "absent.env"))


@pytest.fixture
def recorded_expectations(monkeypatch):
    """Replace `playwright_support.expect` with a recorder, returning the assertions made.

    The condition waits assert through Playwright's own `expect`, which wants a real Locator.
    Recording it instead lets a test see which element each assertion was pointed at, and where
    in the sequence it fell, against the same fake page the other doubles use.
    """
    calls: list[tuple[str, str, dict]] = []

    def fake_expect(locator):
        class _Assertions:
            def to_have_count(self, count, **kwargs):
                calls.append((locator.name, f"to_have_count:{count}", kwargs))
                locator.page.interactions.append(f"expect:{locator.name}:count={count}")

            def to_be_visible(self, **kwargs):
                calls.append((locator.name, "to_be_visible", kwargs))
                locator.page.interactions.append(f"expect:{locator.name}:visible")

        return _Assertions()

    monkeypatch.setattr(playwright_support, "expect", fake_expect)
    return calls


# A METRION per-thread raw CPU export, as `metrion analyze --export-raw-data` writes it.
RAW_EXPORT_HEADER = [
    "interval_start",
    "interval_end",
    "cmdline",
    "executable_name",
    "resource_group_name",
    "resource_group_id",
    "total_energy_j",
    "active_energy_j",
    "idle_energy_j",
]


def write_raw_export(path: Path, rows: list[tuple]) -> None:
    """Rows are `(resource_group_name, total_energy_j)`, or
    `(resource_group_name, resource_group_id, total_energy_j)` when the test needs the
    `resource_group_id` fallback resolution stage."""
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(RAW_EXPORT_HEADER)
        for row in rows:
            group, energy = row[0], row[-1]
            group_id = row[1] if len(row) == 3 else ""
            writer.writerow(["t0", "t1", "cmd", "exe", group, group_id, energy, energy, 0])


def write_detailed_export(path: Path, rows: list[dict]) -> None:
    """Full control over every column, for the active/idle and thread-name paths."""
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_EXPORT_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow({**dict.fromkeys(RAW_EXPORT_HEADER, ""), **row})


SERVER_CGROUP = "/system.slice/docker-abc123def456xx.scope"
CONTAINER_MAP = {"abc123def456": "inventree-server"}
