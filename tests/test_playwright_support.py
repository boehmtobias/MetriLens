"""The shared login helper, its retry budget and the browser lifecycle.

They live outside the per-version scenario scripts so no scenario can fork a copy that drifts.
Everything here runs against the fake page in `conftest.py`, which pins how the code spends a
budget that was tuned on real repetitions.
"""

import playwright_support
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright_support import (
    browser_page,
    choose_related_option,
    expect_dialog_closed,
    expect_refresh,
    expect_write,
    login,
)

from metrilens.credentials import CredentialError

# Every test here calls `login`, which resolves the target app's credentials.
pytestmark = pytest.mark.usefixtures("target_app_credentials")


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip the retry backoff, returning the delays it would have waited."""
    slept: list[float] = []
    monkeypatch.setattr(playwright_support.time, "sleep", slept.append)
    return slept


class TestLoginBudget:
    """The first login round fails in nearly every repetition, so it is priced as a cost being
    paid, not a risk being hedged."""

    def test_the_first_round_waits_briefly_and_later_rounds_do_not(self, fake_page, no_sleep):
        page = fake_page(failing_rounds=2)

        login(page, "http://localhost:8180")

        assert page.redirect_timeouts == [
            playwright_support._FIRST_ROUND_REDIRECT_TIMEOUT_MS,
            playwright_support._LOGIN_REDIRECT_TIMEOUT_MS,
            playwright_support._LOGIN_REDIRECT_TIMEOUT_MS,
        ]
        assert no_sleep == [
            playwright_support._FIRST_ROUND_BACKOFF_S,
            playwright_support._LOGIN_RETRY_BACKOFF_S,
        ]

    def test_a_login_that_works_first_try_costs_nothing_extra(self, fake_page, no_sleep):
        page = fake_page(failing_rounds=0)

        login(page, "http://localhost:8180")

        assert page.redirect_timeouts == [playwright_support._FIRST_ROUND_REDIRECT_TIMEOUT_MS]
        assert no_sleep == []

    def test_the_click_timeout_is_the_same_on_every_round(self, fake_page, no_sleep):
        """Only the redirect wait and the backoff shrink on round one. Shortening the click would
        make the `Enter` fallback the effective primary submit path there."""
        page = fake_page(failing_rounds=2)

        login(page, "http://localhost:8180")

        expected = f"click:{playwright_support._LOGIN_CLICK_TIMEOUT_MS}"
        assert page.submits == [expected, expected, expected]

    def test_the_budget_is_bounded_and_the_last_failure_is_raised(self, fake_page, no_sleep):
        """A login that never redirects has to fail the repetition rather than let the scenario
        run unauthenticated, which would produce a step log full of login pages."""
        page = fake_page(failing_rounds=playwright_support._LOGIN_ATTEMPTS)

        with pytest.raises(PlaywrightTimeoutError):
            login(page, "http://localhost:8180")

        assert len(page.redirect_timeouts) == playwright_support._LOGIN_ATTEMPTS
        assert len(no_sleep) == playwright_support._LOGIN_ATTEMPTS - 1


class TestLoginIsParameterizedByBaseUrl:
    """The whole reason this helper can be shared: the stack it points at is its only
    version-specific input."""

    @pytest.mark.parametrize("base_url", ["http://localhost:8180", "http://localhost:8480"])
    def test_it_logs_in_against_the_base_url_it_is_given(self, base_url, fake_page, no_sleep):
        page = fake_page(failing_rounds=0)

        login(page, base_url)

        assert page.visited == [f"{base_url}/web/login"]

    def test_every_round_revisits_the_same_login_page(self, fake_page, no_sleep):
        page = fake_page(failing_rounds=2)

        login(page, "http://localhost:8480")

        assert page.visited == ["http://localhost:8480/web/login"] * 3


class TestLoginCredentials:
    """The helper names its credential variables and never holds a value."""

    def test_it_fills_the_values_the_named_variables_carry(self, fake_page, no_sleep):
        page = fake_page(failing_rounds=0)

        login(page, "http://localhost:8180")

        assert page.filled["Your username"] == "admin"
        assert page.filled["Your password"] == "secret"

    def test_a_missing_credential_fails_naming_the_variable(self, monkeypatch, fake_page, no_sleep):
        monkeypatch.delenv(playwright_support.ADMIN_PASSWORD_ENV)
        page = fake_page(failing_rounds=0)

        with pytest.raises(CredentialError, match=playwright_support.ADMIN_PASSWORD_ENV):
            login(page, "http://localhost:8180")

        # Fails before touching the browser, so a missing setting reads as a missing setting
        # rather than as a login timeout.
        assert page.visited == []


class TestBrowserPage:
    def test_it_yields_a_page_and_closes_the_browser(self, fake_browser, patch_playwright):
        patch_playwright(fake_browser)

        with browser_page() as page:
            assert page in fake_browser.pages
            assert not fake_browser.closed

        assert fake_browser.closed

    def test_the_browser_closes_even_when_the_body_raises(self, fake_browser, patch_playwright):
        """Closing inline would let any step that raises skip the close and leak Chromium's
        temporary profile."""
        patch_playwright(fake_browser)

        with pytest.raises(RuntimeError, match="step failed"):
            with browser_page():
                raise RuntimeError("step failed")

        assert fake_browser.closed

    @pytest.mark.parametrize("headless", [True, False])
    def test_it_launches_chromium_headless_unless_told_otherwise(
        self, fake_browser, patch_playwright, headless
    ):
        launched = patch_playwright(fake_browser)

        with browser_page(headless=headless):
            pass

        assert launched == [{"headless": headless}]

    def test_it_defaults_to_headless(self, fake_browser, patch_playwright):
        """A measured repetition is never watched, and a headed browser would change what the
        repetition costs, so the visible mode stays an explicit debugging opt-in."""
        launched = patch_playwright(fake_browser)

        with browser_page():
            pass

        assert launched == [{"headless": True}]


class TestResponseMatcher:
    """The response predicate, which is where a wrong wait would actually live: the rest of each
    helper is one Playwright call."""

    def test_it_matches_the_method_and_the_fragment(self, fake_response):
        matches = playwright_support._response_matcher("/api/stock/", "POST")

        assert matches(fake_response("http://h/api/stock/", "POST", 201))
        assert not matches(fake_response("http://h/api/stock/", "GET", 200))
        assert not matches(fake_response("http://h/api/build/", "POST", 201))

    @pytest.mark.parametrize("status", [200, 302])
    def test_a_successful_write_satisfies_it(self, status, fake_response):
        matches = playwright_support._response_matcher("/issue/", "POST")

        assert matches(fake_response("http://h/api/build/2/issue/", "POST", status))

    @pytest.mark.parametrize("status", [400, 500])
    def test_a_refused_write_never_satisfies_it(self, status, fake_response):
        """The load-bearing half. Without the status test a rejected submit would satisfy the
        wait, the script would carry on against a dialog still holding a validation error, and
        the repetition would die several actions later with an unrelated message."""
        matches = playwright_support._response_matcher("/issue/", "POST")

        assert not matches(fake_response("http://h/api/build/2/issue/", "POST", status))


class TestExpectWriteAndRefresh:
    def test_a_write_waits_for_the_post_and_a_refresh_for_the_get(self, fake_page, fake_response):
        page = fake_page()

        with expect_write(page, "/api/stock/"):
            pass
        with expect_refresh(page, "/api/build/line/"):
            pass

        write_predicate, write_timeout = page.awaited_responses[0]
        refresh_predicate, refresh_timeout = page.awaited_responses[1]
        assert write_predicate(fake_response("http://h/api/stock/", "POST", 201))
        assert not write_predicate(fake_response("http://h/api/stock/", "GET", 200))
        assert refresh_predicate(fake_response("http://h/api/build/line/?build=2", "GET", 200))
        assert not refresh_predicate(fake_response("http://h/api/build/line/", "POST", 201))
        assert write_timeout == refresh_timeout == playwright_support._CONDITION_TIMEOUT_MS

    def test_both_waits_open_before_the_click_they_bracket(self, fake_page):
        """A wait opened after the click would race the response it is waiting for, which on a
        local stack answers in tens of milliseconds."""
        page = fake_page()

        with expect_refresh(page, "/api/build/"), expect_write(page, "/finish/"):
            page.get_by_role("button", name="Submit").click()

        assert page.interactions == [
            "await_open:0",
            "await_open:1",
            "click:button:Submit",
            "await_close:1",
            "await_close:0",
        ]


class TestExpectDialogClosed:
    def test_it_asserts_no_dialog_remains(self, fake_page, recorded_expectations):
        page = fake_page()

        expect_dialog_closed(page)

        timeout = playwright_support._CONDITION_TIMEOUT_MS
        assert recorded_expectations == [("dialog:None", "to_have_count:0", {"timeout": timeout})]


class TestChooseRelatedOption:
    def test_it_waits_for_the_search_before_selecting(self, fake_page, recorded_expectations):
        page = fake_page()

        choose_related_option(page, "related-field-category", "MetriLens Fixtures")

        assert page.interactions == [
            "click:related-field-category",
            "await_open:0",
            "type:MetriLens Fixtures",
            "await_close:0",
            "expect:option:MetriLens Fixtures:visible",
            "press:ArrowDown",
            "press:Enter",
        ]

    def test_the_wait_is_on_the_filtered_search_not_the_unfiltered_one(
        self, fake_page, fake_response, recorded_expectations
    ):
        """The regression this helper exists for.

        Opening a related-field dropdown issues an unfiltered search first, and on this
        scenario's seed data the target is the only entry that unfiltered list returns. So a
        wait keyed on the option being visible, or on any search response, is already satisfied
        before the typed search is issued, and the helper would select against a stale list
        while appearing to wait.
        """
        page = fake_page()

        choose_related_option(page, "related-field-category", "MetriLens Fixtures")
        predicate, _ = page.awaited_responses[0]

        unfiltered = "http://h/api/part/category/?structural=false&search=&offset=0&limit=10"
        filtered = "http://h/api/part/category/?structural=false&search=MetriLens+Fixtures"
        assert not predicate(fake_response(unfiltered, "GET", 200))
        assert predicate(fake_response(filtered, "GET", 200))

    def test_the_search_term_is_url_encoded(self, fake_page, fake_response, recorded_expectations):
        """`search=Core Flow Output` never appears on the wire; the wait has to match what does."""
        page = fake_page()

        choose_related_option(page, "related-field-location", "Core Flow Output")
        predicate, _ = page.awaited_responses[0]

        assert predicate(fake_response("http://h/api/stock/location/?search=Core+Flow+Output"))
        assert not predicate(fake_response("http://h/api/stock/location/?search=Core Flow Output"))
