"""The illustrative recording path, and the gate that keeps it out of measurement.

The tests that matter most here are the negative ones: with no recording environment set, a
repetition must behave exactly as it did before this module existed, because the screencast is
a CPU load that varies with what is on screen and would perturb the energy figure it is meant
to illustrate. Everything else is the contract between `metrilens record` and the scenario
subprocess it launches, which is carried in environment variables and so cannot be type-checked.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright_support import browser_page

from metrilens import recording
from metrilens.orchestrator import _run_script
from metrilens.scenario_logger import ScenarioLogger


@pytest.fixture
def recording_env(monkeypatch, tmp_path):
    """Put the process in the state `metrilens record` puts a scenario subprocess in."""

    def _enable(label="DEBUG=True, cache off", captions=True):
        for name, value in recording.environment(tmp_path, label, captions).items():
            monkeypatch.setenv(name, value)
        return tmp_path

    return _enable


class TestTheGate:
    """Nothing in this module may do anything unless `metrilens record` asked for it."""

    @pytest.mark.parametrize("enabled", [False, True])
    def test_is_enabled_follows_the_environment(self, monkeypatch, recording_env, enabled):
        if enabled:
            recording_env()
        else:
            monkeypatch.delenv(recording.VIDEO_DIR_ENV, raising=False)

        assert recording.is_enabled() is enabled

    def test_a_measured_run_gets_a_plain_page_and_no_context(
        self, monkeypatch, fake_browser, patch_playwright
    ):
        """The measured path stays the `new_page()` it was before this feature existed."""
        monkeypatch.delenv(recording.VIDEO_DIR_ENV, raising=False)
        patch_playwright(fake_browser)

        with browser_page() as page:
            assert page.video is None

        assert fake_browser.contexts == []

    def test_a_caption_outside_a_recording_does_nothing(self, monkeypatch):
        """`ScenarioLogger.step` calls this on every step of every measured repetition."""
        monkeypatch.delenv(recording.VIDEO_DIR_ENV, raising=False)

        recording.caption("create_part")  # no page, no browser, no failure


class TestTheSubprocessContract:
    """The names in `environment()` are the only channel to a scenario script, which runs as
    its own process; a typo here is a feature that silently does nothing."""

    @pytest.mark.parametrize(("captions", "expected_flag"), [(True, "1"), (False, "0")])
    def test_it_carries_the_directory_the_label_and_the_caption_switch(
        self, tmp_path, captions, expected_flag
    ):
        assert recording.environment(tmp_path, "DEBUG=False", captions=captions) == {
            recording.VIDEO_DIR_ENV: str(tmp_path),
            recording.LABEL_ENV: "DEBUG=False",
            recording.CAPTIONS_ENV: expected_flag,
        }

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("DEBUG=True", "debug_true"),
            ("DEBUG=True, cache off", "debug_true_cache_off"),
            ("  spaced  out  ", "spaced_out"),
            ("...", "recording"),
        ],
    )
    def test_the_video_is_named_for_the_app_version(self, tmp_path, label, expected):
        assert recording.video_path(tmp_path, label) == tmp_path / f"{expected}.webm"


class TestRecordedPage:
    def test_it_records_at_the_viewport_a_measured_run_uses(
        self, recording_env, fake_browser, patch_playwright
    ):
        """Recording at Chromium's own default size means the video shows the layout the
        measured repetitions actually drive, not a differently-reflowed one."""
        video_dir = recording_env()
        patch_playwright(fake_browser)

        with browser_page():
            pass

        (context,) = fake_browser.contexts
        assert context.kwargs == {
            "viewport": {"width": 1280, "height": 720},
            "record_video_dir": str(video_dir),
            "record_video_size": {"width": 1280, "height": 720},
        }

    def test_the_video_is_saved_under_the_label_before_the_context_closes(
        self, recording_env, fake_browser, patch_playwright
    ):
        """Ordering is the whole of whether a playable file exists: the page has to close
        before the video is complete, and `save_as` has to run before the context goes."""
        video_dir = recording_env(label="DEBUG=False")
        patch_playwright(fake_browser)

        with browser_page() as page:
            pass

        (context,) = fake_browser.contexts
        assert page.closed
        assert page.video.saved_to == str(video_dir / "debug_false.webm")
        assert page.video.deleted
        assert context.closed
        assert fake_browser.closed

    def test_the_overlay_is_installed_for_every_document(
        self, recording_env, fake_browser, patch_playwright
    ):
        """An init script rather than a one-off injection, because the scenario scripts
        navigate in nearly every step and anything appended to one document would be gone."""
        recording_env(label="DEBUG=True")
        patch_playwright(fake_browser)

        with browser_page():
            pass

        (context,) = fake_browser.contexts
        (script,) = context.init_scripts
        assert "'DEBUG=True'" in script
        assert "pointer-events:none" in script

    def test_no_captions_means_no_overlay_but_still_a_video(
        self, recording_env, fake_browser, patch_playwright
    ):
        recording_env(captions=False)
        patch_playwright(fake_browser)

        with browser_page() as page:
            pass

        (context,) = fake_browser.contexts
        assert context.init_scripts == []
        assert page.video.saved_to is not None


class TestCaption:
    def test_it_shows_the_step_on_the_page_being_recorded(
        self, recording_env, fake_browser, patch_playwright
    ):
        recording_env()
        patch_playwright(fake_browser)

        with browser_page() as page:
            recording.caption("create_part")

        (_, text) = page.evaluated[-1]
        assert text == "create_part"

    def test_an_iteration_is_named_alongside_the_step(
        self, recording_env, fake_browser, patch_playwright
    ):
        """`inventree_session_flow` runs the same step ids several times per repetition, so
        the step id alone would not say where in the run a frame is."""
        recording_env()
        patch_playwright(fake_browser)

        with browser_page() as page:
            recording.caption("adjust_stock", iteration=3)

        (_, text) = page.evaluated[-1]
        assert text.startswith("adjust_stock")
        assert "3" in text

    def test_the_page_is_released_when_the_recording_ends(
        self, recording_env, fake_browser, patch_playwright
    ):
        recording_env()
        patch_playwright(fake_browser)

        with browser_page():
            pass
        recording.caption("create_part")

        assert recording._active_page is None

    def test_a_caption_that_fails_never_fails_the_run(
        self, recording_env, fake_browser, patch_playwright
    ):
        """A navigation in flight can make `evaluate` raise, and a presentation detail must
        not be able to kill the run it is presenting."""
        recording_env()
        patch_playwright(fake_browser)

        with browser_page() as page:
            page.evaluate = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("navigating"))
            recording.caption("create_part")

    def test_captions_off_leaves_the_page_alone(
        self, recording_env, fake_browser, patch_playwright
    ):
        recording_env(captions=False)
        patch_playwright(fake_browser)

        with browser_page() as page:
            recording.caption("create_part")

        assert page.evaluated == []


class TestStepBoundaries:
    """The caption is driven by the same boundary the step log records, rather than by
    timestamps matched up against the video afterwards."""

    def test_opening_a_step_captions_it(self, monkeypatch, tmp_path):
        captioned: list[tuple[str, int | None]] = []
        monkeypatch.setattr(
            "metrilens.scenario_logger.recording.caption",
            lambda step_id, iteration=None: captioned.append((step_id, iteration)),
        )
        logger = ScenarioLogger(["create_part"], tmp_path / "step_log.json")

        with logger.step("create_part", iteration=2):
            pass

        assert captioned == [("create_part", 2)]

    def test_an_undeclared_step_is_never_captioned(self, monkeypatch, tmp_path):
        """The validation is the scenario's, and a caption must not appear for a step the
        scenario refuses to log."""
        captioned: list[tuple[str, int | None]] = []
        monkeypatch.setattr(
            "metrilens.scenario_logger.recording.caption",
            lambda step_id, iteration=None: captioned.append((step_id, iteration)),
        )
        logger = ScenarioLogger(["create_part"], tmp_path / "step_log.json")

        with pytest.raises(Exception, match="not declared"):
            with logger.step("wrong_step"):
                pass

        assert captioned == []


class TestTheScriptEnvironment:
    """The recorder configures a subprocess, so the merge below is what actually decides
    whether a recorded run can log in at all."""

    @pytest.fixture
    def recorded_run(self, monkeypatch):
        """Capture the environment `_run_script` hands to the scenario subprocess."""
        calls: list[dict | None] = []

        def fake_run(command, **kwargs):
            calls.append(kwargs["env"])
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr("metrilens.orchestrator.subprocess.run", fake_run)
        return calls

    def test_the_recording_variables_are_added_to_the_inherited_environment(
        self, monkeypatch, recorded_run, tmp_path
    ):
        """Never substituted for it: the script resolves the target app's credentials from
        the environment it is handed, so replacing it outright would fail every login."""
        monkeypatch.setenv("INVENTREE_ADMIN_USER", "admin")

        _run_script("script.py", tmp_path / "step_log.json", extra_env={"X_RECORD": "1"})

        (env,) = recorded_run
        assert env["INVENTREE_ADMIN_USER"] == "admin"
        assert env["X_RECORD"] == "1"

    def test_a_measured_repetition_inherits_the_environment_untouched(self, recorded_run, tmp_path):
        _run_script("script.py", tmp_path / "step_log.json")

        assert recorded_run == [None]


class TestSlowCopy:
    """The companion video, which is post-processing on a capture that has already succeeded."""

    @pytest.fixture
    def ffmpeg(self, monkeypatch, tmp_path):
        """A stand-in binary, plus the browsers path Playwright's own build would sit under."""
        binary = tmp_path / "ms-playwright" / "ffmpeg-1011" / "ffmpeg-linux"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "ms-playwright"))
        return binary

    @pytest.fixture
    def ffmpeg_calls(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr("metrilens.recording.subprocess.run", fake_run)
        return calls

    def test_it_prefers_playwrights_own_bundled_build(self, ffmpeg):
        """Which is what keeps this dependency-free: Playwright installed it to encode the
        screencast, and re-timing a stream needs nothing the bundled build lacks."""
        assert recording.find_ffmpeg() == ffmpeg

    def test_it_falls_back_to_a_system_ffmpeg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "absent"))
        monkeypatch.setattr("metrilens.recording.shutil.which", lambda name: "/usr/bin/ffmpeg")

        assert recording.find_ffmpeg() == Path("/usr/bin/ffmpeg")

    def test_the_stream_is_re_timed_not_re_encoded(self, tmp_path, ffmpeg, ffmpeg_calls):
        """`-itsscale` plus a stream copy is what makes this lossless and effectively free;
        a re-encode would cost a minute per video and change the frames."""
        target = recording.slow_copy(tmp_path / "debug_false.webm", factor=4)

        (command,) = ffmpeg_calls
        assert command[:2] == [str(ffmpeg), "-y"]
        assert "-itsscale" in command and "4" in command
        assert command[-3:-1] == ["-c", "copy"]
        assert target == tmp_path / "debug_false.slow4x.webm"

    def test_the_original_is_never_the_target(self, tmp_path, ffmpeg, ffmpeg_calls):
        """The capture is the honest record of what the automation did, and every other speed
        can be re-derived from it without re-running a scenario."""
        video = tmp_path / "debug_false.webm"

        target = recording.slow_copy(video, factor=2)

        assert target != video
        assert str(video) in ffmpeg_calls[0]

    def test_quarter_speed_is_the_default(self, tmp_path, ffmpeg, ffmpeg_calls):
        target = recording.slow_copy(tmp_path / "debug_false.webm")

        assert recording.DEFAULT_SLOWDOWN == 4.0
        assert target.name == "debug_false.slow4x.webm"

    def test_a_missing_ffmpeg_is_reported_rather_than_raised_blindly(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "absent"))
        monkeypatch.setattr("metrilens.recording.shutil.which", lambda name: None)

        with pytest.raises(recording.RecordingError, match="no ffmpeg found"):
            recording.slow_copy(tmp_path / "debug_false.webm")

    @pytest.mark.parametrize("factor", [1, 0])
    def test_a_factor_that_would_not_slow_anything_is_refused(self, tmp_path, ffmpeg, factor):
        with pytest.raises(recording.RecordingError, match="greater than 1"):
            recording.slow_copy(tmp_path / "debug_false.webm", factor=factor)

    def test_an_ffmpeg_failure_carries_its_own_reason(self, monkeypatch, tmp_path, ffmpeg):
        monkeypatch.setattr(
            "metrilens.recording.subprocess.run",
            lambda command, **kwargs: SimpleNamespace(returncode=1, stderr="Invalid data"),
        )

        with pytest.raises(recording.RecordingError, match="Invalid data"):
            recording.slow_copy(tmp_path / "debug_false.webm")
