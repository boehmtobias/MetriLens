"""Illustrative screen recording of a scenario run, kept out of the measured path.

Produces a video of what the scenario does, for readers who have never seen the target
application. It is never a measurement:

- Nothing here runs unless `METRILENS_RECORD_VIDEO_DIR` is set, which only `metrilens record`
  does. In a measured repetition `is_enabled()` is False and the call sites are no-ops.
- Screencast encoding adds a CPU load that varies with what is on screen, so it would distort
  a measured repetition's energy, and not by a constant offset.
- A recorded run is slower than the same run unrecorded, and writes no `repetition` row.

Captions are rendered in the page rather than added to the video afterwards, because
Playwright's bundled ffmpeg has no `drawtext` filter. This also keeps each caption in the same
frame as the UI it describes.
"""

import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page

# The recorder's entire contract with the scenario script it runs, which is a subprocess and
# so can only be configured through the environment (the script's own CLI belongs to the
# orchestrator, and adding flags there would mean editing every version script identically).
VIDEO_DIR_ENV = "METRILENS_RECORD_VIDEO_DIR"
LABEL_ENV = "METRILENS_RECORD_LABEL"
CAPTIONS_ENV = "METRILENS_RECORD_CAPTIONS"

# Chromium's own default viewport, which is what an unrecorded repetition runs at. Keeping the
# recording at the same size means the video shows the layout the measured runs actually drive,
# rather than a differently-reflowed one.
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720

# How much slower the companion copy plays. The automated scenario runs too fast to follow;
# quarter speed is slow enough to tell what each step did. The frames are the same ones held
# longer (see `slow_copy`), so a larger factor costs only effective frame rate.
DEFAULT_SLOWDOWN = 4.0


class RecordingError(RuntimeError):
    """Raised for a failure in the post-processing that follows a successful capture.

    Separate from the capture itself on purpose: by the time these can fire, a stack has been
    reset and a scenario has run, so the caller's job is to keep the video it already has and
    say what did not happen on top of it.
    """


_OVERLAY_ID = "metrilens-recording-overlay"
_CAPTION_STORAGE_KEY = "__metrilens_caption__"

# The page currently being recorded, set by `recorded_page` and read by `caption`.
#
# A module-level handle rather than a parameter because the two call sites sit in different
# objects: the context is created in `playwright_support.browser_page`, while the step
# boundaries are known only to `ScenarioLogger`, which has no page and should not grow one.
# Passing a page through the logger would mean changing every version script's construction
# of it, and those scripts are asserted identical to each other; a handle that is None unless
# recording is enabled keeps them untouched.
_active_page: Page | None = None


def is_enabled() -> bool:
    """True only inside a `metrilens record` run."""
    return bool(os.environ.get(VIDEO_DIR_ENV))


def captions_enabled() -> bool:
    """Whether to draw the caption overlay; `--no-captions` sets this to "0" for clean footage."""
    return os.environ.get(CAPTIONS_ENV, "1") != "0"


def environment(video_dir: Path, label: str, captions: bool) -> dict[str, str]:
    """The variables `metrilens record` adds to a scenario script's environment.

    Built here rather than in the CLI so the names above have exactly one writer and one
    reader, and a change to the contract cannot be half-applied.
    """
    return {
        VIDEO_DIR_ENV: str(video_dir),
        LABEL_ENV: label,
        CAPTIONS_ENV: "1" if captions else "0",
    }


def video_path(video_dir: Path, label: str) -> Path:
    """Where the finished video lands: named for the app version, not for Playwright's hash."""
    return video_dir / f"{slug(label)}.webm"


def slug(label: str) -> str:
    """Filename-safe form of an `app_version.label` such as "DEBUG=True, cache off"."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", label.lower())).strip("_") or "recording"


def _overlay_script(label: str) -> str:
    """The init script that draws the caption bar, re-run by Chromium on every document.

    Registered with `add_init_script` rather than injected once, because the scenario scripts
    navigate constantly (`page.goto` in nearly every step) and anything appended to a single
    document would vanish on the next one. The caption text survives those navigations in
    `localStorage`, same origin throughout a run, so a fresh document restores the current
    caption on its own without Python having to re-push it after every navigation.

    `pointer-events: none` is load-bearing: the bar sits above the app at the maximum z-index,
    and without it the bar could become the hit target for a click underneath, which is exactly
    the actionability failure Playwright would report as a timeout on an unrelated element.
    """
    return f"""
    (() => {{
      const ID = {_OVERLAY_ID!r};
      const KEY = {_CAPTION_STORAGE_KEY!r};
      const LABEL = {label!r};

      function render() {{
        const root = document.documentElement;
        if (!root) return;
        let bar = document.getElementById(ID);
        if (!bar) {{
          bar = document.createElement('div');
          bar.id = ID;
          bar.style.cssText = [
            'position:fixed', 'left:0', 'right:0', 'bottom:0', 'z-index:2147483647',
            'pointer-events:none', 'display:flex', 'justify-content:space-between',
            'gap:24px', 'padding:10px 18px', 'box-sizing:border-box',
            'background:rgba(17,17,17,0.82)', 'color:#fff',
            'font:600 16px/1.3 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
            'letter-spacing:0.02em'
          ].join(';');
          const version = document.createElement('span');
          version.id = ID + '-label';
          version.textContent = LABEL;
          const step = document.createElement('span');
          step.id = ID + '-step';
          step.style.cssText = 'opacity:0.92';
          bar.appendChild(version);
          bar.appendChild(step);
          root.appendChild(bar);
        }}
        let text = '';
        try {{ text = window.localStorage.getItem(KEY) || ''; }} catch (e) {{}}
        document.getElementById(ID + '-step').textContent = text;
      }}

      window.__metrilensCaption = (text) => {{
        try {{ window.localStorage.setItem(KEY, text); }} catch (e) {{}}
        render();
      }};

      if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', render);
      }} else {{
        render();
      }}
    }})();
    """


def caption(step_id: str, iteration: int | None = None) -> None:
    """Show `step_id` in the overlay, as the step it names opens. No-op unless recording.

    Every failure is swallowed deliberately. A caption is a presentation detail, and a run that
    dies because a navigation happened to be in flight while the overlay was being updated would
    be a recording concern breaking the very thing it is trying to show.
    """
    if _active_page is None or not captions_enabled():
        return
    text = step_id if iteration is None else f"{step_id}  ·  iteration {iteration}"
    try:
        _active_page.evaluate(
            "text => window.__metrilensCaption && window.__metrilensCaption(text)", text
        )
    except Exception:  # noqa: BLE001 - see docstring: never fail a run over a caption
        pass


@contextmanager
def recorded_page(browser: Browser) -> Iterator[Page]:
    """Yield a page whose session is being recorded, saving the video under `VIDEO_DIR_ENV`.

    Playwright attaches video to the *context*, not to the browser, so this creates one
    explicitly where an unrecorded run just calls `browser.new_page()`. The ordering in the
    `finally` is what actually produces a playable file: the page has to close before the
    video is complete, `save_as` then waits for the encoder and writes it where we want it
    rather than under Playwright's hashed name, and only then is the context disposable.
    """
    global _active_page

    video_dir = Path(os.environ[VIDEO_DIR_ENV])
    video_dir.mkdir(parents=True, exist_ok=True)
    label = os.environ.get(LABEL_ENV, "")

    context: BrowserContext = browser.new_context(
        viewport={"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
        record_video_dir=str(video_dir),
        record_video_size={"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
    )
    if captions_enabled():
        context.add_init_script(_overlay_script(label))
    page = context.new_page()
    _active_page = page
    try:
        yield page
    finally:
        _active_page = None
        video = page.video
        page.close()
        if video is not None:
            video.save_as(video_path(video_dir, label))
            video.delete()
        context.close()


def find_ffmpeg() -> Path | None:
    """The ffmpeg to post-process with: Playwright's own bundled build, else a system one.

    Preferring the bundled build is what keeps this dependency-free, since Playwright has
    already installed it to encode the screencast in the first place. It is compiled
    `--disable-everything` and can encode nothing but VP8, which is exactly enough for
    `slow_copy`: that re-times a stream rather than re-encoding it.
    """
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    root = Path(browsers_path) if browsers_path else Path.home() / ".cache" / "ms-playwright"
    for candidate in sorted(root.glob("ffmpeg-*/ffmpeg-*")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    system_ffmpeg = shutil.which("ffmpeg")
    return Path(system_ffmpeg) if system_ffmpeg else None


def slow_copy(video: Path, factor: float = DEFAULT_SLOWDOWN) -> Path:
    """Write a companion video that plays `factor` times slower, beside the original.

    `-itsscale` scales the container's timestamps and the stream is copied, not re-encoded:
    the output holds the same frames for longer. That makes this exact, lossless and
    effectively free (milliseconds for a 5 MB capture, versus a minute to re-encode), and it
    is also why no interpolation happens: quarter speed plays the 25 fps capture at an
    effective 6.25 fps, which a screencast of discrete UI states carries fine.

    The original is kept rather than replaced. It is the honest record of what the automation
    actually did, and any other factor can be re-derived from it without re-running anything.
    """
    if factor <= 1:
        raise RecordingError(f"slowdown factor must be greater than 1, got {factor}")
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise RecordingError(
            "no ffmpeg found (looked for Playwright's bundled build under "
            "PLAYWRIGHT_BROWSERS_PATH or ~/.cache/ms-playwright, then on PATH)"
        )
    target = video.with_suffix(f".slow{factor:g}x.webm")
    result = subprocess.run(
        [
            str(ffmpeg),
            "-y",
            "-itsscale",
            f"{factor:g}",
            "-i",
            str(video),
            "-c",
            "copy",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RecordingError(f"ffmpeg exited {result.returncode}: {result.stderr.strip()[-300:]}")
    return target
