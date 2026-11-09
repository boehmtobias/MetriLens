"""Shared Playwright support for the InvenTree scenario scripts.

The login helper, its retry budget and the browser lifecycle are shared across every
version script rather than copied into each: measurement overhead has to be identical
across the versions being compared, and a helper imported once cannot drift the way
copies can.

Everything here is parameterized by base URL and by nothing else, so a script's only
version-specific input stays the stack it points at.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import quote_plus

from playwright.sync_api import Browser, Page, Response, expect, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from metrilens import recording
from metrilens.credentials import require_env

# The credentials themselves come from the environment, never from a scenario file: only the
# variable *names* live here, resolved inside `login` rather than at import time, so importing
# a script module (which the scenario-definition tests do) needs no credentials present.
ADMIN_USERNAME_ENV = "INVENTREE_ADMIN_USER"
ADMIN_PASSWORD_ENV = "INVENTREE_ADMIN_PASSWORD"

# Retry budget for the login. The failure being retried is not app coldness: `wait_for_readiness`
# already proves the auth path works before a scenario script starts, but a fresh Chromium
# process still tends to fail its first login attempt against a warm app regardless, either the
# "Log In" click's actionability wait never resolving, or the click landing but the following
# authenticated request coming back unauthorized so the SPA never redirects. A later attempt in
# the same process typically succeeds quickly.
#
# So the budget favors many short attempts over few long ones, tuned to make one bad round cheap
# rather than to wait out a warm-up.
_LOGIN_ATTEMPTS = 6
_LOGIN_RETRY_BACKOFF_S = 1.0
_LOGIN_CLICK_TIMEOUT_MS = 5000
_LOGIN_REDIRECT_TIMEOUT_MS = 6000

# The first round is priced separately, since round one failing is the normal case here rather
# than an exception to hedge against; spending the full redirect timeout on it wastes time on
# something already known not to happen. A shorter first-round timeout still leaves generous
# headroom over how long a successful redirect actually takes, so a genuinely slow-but-working
# login costs one extra cheap round rather than a lost repetition.
#
# Only the redirect wait and the backoff shrink for round one; the click timeout stays constant
# across every round, so shortening it wouldn't make the `Enter` fallback below the effective
# primary submit path on round one.
_FIRST_ROUND_REDIRECT_TIMEOUT_MS = 2000
_FIRST_ROUND_BACKOFF_S = 0.2


def login(page: Page, base_url: str) -> None:
    """Log into the InvenTree instance at `base_url`, retrying under the budget above.

    Raises Playwright's `TimeoutError` if the final round still does not redirect, which
    fails the repetition rather than letting the scenario run unauthenticated.
    """
    username = require_env(ADMIN_USERNAME_ENV)
    password = require_env(ADMIN_PASSWORD_ENV)
    for attempt in range(1, _LOGIN_ATTEMPTS + 1):
        first_round = attempt == 1
        page.goto(f"{base_url}/web/login")
        page.get_by_placeholder("Your username").fill(username)
        password_field = page.get_by_placeholder("Your password")
        password_field.fill(password)
        try:
            page.get_by_role("button", name="Log In").click(timeout=_LOGIN_CLICK_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            # The click is kept as the primary path because it is the one proven to work; this
            # is only the escape hatch for the case where Playwright waits for the button to be
            # "stable" and never gets there even though the element is visible, enabled,
            # unobstructed, and nothing is loading. A keypress on the focused field submits the
            # same form without an actionability wait.
            password_field.press("Enter")
        try:
            page.wait_for_url(
                "**/web/home",
                timeout=_FIRST_ROUND_REDIRECT_TIMEOUT_MS
                if first_round
                else _LOGIN_REDIRECT_TIMEOUT_MS,
            )
            return
        except PlaywrightTimeoutError:
            if attempt == _LOGIN_ATTEMPTS:
                raise
            time.sleep(_FIRST_ROUND_BACKOFF_S if first_round else _LOGIN_RETRY_BACKOFF_S)


# Condition waits, used instead of fixed sleeps in a scenario script.
#
# Every wait below is on the actual event a fixed sleep would only approximate: the API call for a
# write, the filtered search for a dropdown, the modal unmounting for a re-render. Deliberately
# *not* `wait_for_load_state("networkidle")`, which is flaky and would also behave differently
# per app version, since `DEBUG=True` serves a dev bundle with a different request count, so it
# would wait on the very thing being measured.
#
# 10 s matches the scripts' `wait_for_url` timeouts. A condition that has not
# arrived by then is a repetition that failed rather than one that ran slowly, which is the
# outcome the orchestrator's retry path is built for.
_CONDITION_TIMEOUT_MS = 10_000


def _response_matcher(url_fragment: str, method: str):
    """Predicate for `page.expect_response`, matched on method, URL fragment and success.

    The status test is what makes a rejected write fail here rather than three actions later:
    a 400 never satisfies the wait, so the repetition dies at the submit that was refused
    instead of at some confusing timeout further down the step.
    """

    def matches(response: Response) -> bool:
        return (
            response.request.method == method
            and url_fragment in response.url
            and response.status < 400
        )

    return matches


def expect_write(page: Page, url_fragment: str):
    """Wait for the write a submit issues, as `with expect_write(page, "/api/stock/"): ...`."""
    return page.expect_response(
        _response_matcher(url_fragment, "POST"), timeout=_CONDITION_TIMEOUT_MS
    )


def expect_refresh(page: Page, url_fragment: str):
    """Wait for the read the UI issues once a write, and any background task behind it, has landed.

    Needed where the POST only queues work: InvenTree's auto-allocate and build-completion
    endpoints answer immediately, the UI then polls `/api/background-task/` and re-reads the
    affected table when that reports done. The re-read, not the POST, shows the work actually
    happened.
    """
    return page.expect_response(
        _response_matcher(url_fragment, "GET"), timeout=_CONDITION_TIMEOUT_MS
    )


def expect_dialog_closed(page: Page) -> None:
    """Assert the modal has unmounted, which is a submit's UI-state half.

    Worth asserting even though the next action would wait for its own target anyway: a submit
    the server refused leaves the dialog open carrying the validation error, and failing on that
    names the real problem instead of surfacing as an unrelated element never becoming clickable.
    """
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=_CONDITION_TIMEOUT_MS)


def choose_related_option(page: Page, field_label: str, option_name: str) -> None:
    """Pick `option_name` in a related-field dropdown, once the *filtered* list has arrived.

    The wait is on the search response rather than on the option being visible. Opening the
    dropdown issues an unfiltered search first, and on this scenario's seed data each of the
    three targets is the sole entry in that unfiltered list, so the option is visible before the
    typed search is even issued. Keyed on the encoded search term, the response can only be the
    filtered one. The visibility assertion stays after it, because the response arriving does
    not mean the list has re-rendered.
    """
    page.get_by_label(field_label).click()
    with page.expect_response(
        _response_matcher(f"search={quote_plus(option_name)}", "GET"),
        timeout=_CONDITION_TIMEOUT_MS,
    ):
        page.keyboard.type(option_name)
    expect(page.get_by_role("option", name=option_name)).to_be_visible(
        timeout=_CONDITION_TIMEOUT_MS
    )
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")


@contextmanager
def browser_page(headless: bool = True) -> Iterator[Page]:
    """Launch Chromium, yield one page, and close the browser however the body ends.

    The `finally` is the point: closing inline instead would mean any step that raised
    skipped the close and leaked Chromium's temporary profile.

    A script's `logger.finalize()` deliberately stays outside this block: a run that
    failed part way through has no complete step log to write, and the orchestrator
    already treats a missing one as a failed repetition.

    The recording branch is reached only under `metrilens record`, never in a measured
    repetition: `recording.is_enabled()` reads an environment variable that only that
    command sets, so a measured repetition uses a plain `new_page()` with no screencast cost
    (see `recording`).
    """
    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=headless)
        try:
            if recording.is_enabled():
                with recording.recorded_page(browser) as page:
                    yield page
            else:
                yield browser.new_page()
        finally:
            browser.close()
