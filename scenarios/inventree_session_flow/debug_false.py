"""Playwright script for the session flow scenario, DEBUG=False.

Realizes `scenario.yaml` in this directory against the DEBUG=False
stack. Portable and run-identity-agnostic: the orchestrator supplies where the step log is
written, and this script has no knowledge of which repetition it is running as.

Identical to the other three version scripts apart from `BASE_URL` and this docstring, so
measurement overhead is the same for every version; a test checks this.
"""

import re
from datetime import datetime
from pathlib import Path

import click
import yaml
from playwright.sync_api import Page, expect
from playwright_support import (
    browser_page,
    choose_related_option,
    expect_dialog_closed,
    expect_refresh,
    expect_write,
    login,
)

from metrilens.scenario_logger import ScenarioLogger

BASE_URL = "http://localhost:8180"

# The scenario definition is the single source of truth for both the declared step ids and k,
# rather than constants repeated here. `scenario.yaml` sits beside this file and is the same
# file `metrilens scenario load` reads, so the script cannot run a k the row does not record.
# Read at import from `__file__`'s directory, since the orchestrator invokes this as a
# subprocess whose working directory is its own.
_DEFINITION = yaml.safe_load((Path(__file__).parent / "scenario.yaml").read_text())
STEPS = _DEFINITION["steps"]
ITERATIONS = _DEFINITION["iterations"]

# The catalog `reset_script` seeds, addressed by name rather than by id. Ids are deterministic
# here, since every repetition starts from a wiped database seeded in a fixed order, but a name
# is what a reader can check against the reset script and what survives a reordering of it.
_CATALOG_ROOT = "MetriLens Catalog"
_CATALOG_CATEGORY = "Catalog Resistors"
_CATALOG_SEARCH = "Connectors"

_STOCK_QUANTITY = 100

# Interaction 8 lands here. Asserted as UI state on top of the API wait, since "the detail
# request came back" and "the detail page is showing" are not the same thing.
_PART_DETAIL_PATTERN = re.compile(r"/web/part/\d+/details")


def _create_part(page: Page, logger: ScenarioLogger, iteration: int, part_name: str) -> str:
    """Create a part in the MetriLens Fixtures category. Returns its part id."""
    with logger.step("create_part", iteration=iteration):
        page.goto(f"{BASE_URL}/web/part/category/index/parts")
        page.get_by_label("panel-tabs-partcategory").get_by_role("tab", name="Parts").click()
        logger.action()

        page.get_by_label("action-menu-add-parts").click()
        page.get_by_text("Create Part", exact=True).click()
        logger.action()

        choose_related_option(page, "related-field-category", "MetriLens Fixtures")
        logger.action()

        page.get_by_label("text-field-name").fill(part_name)
        page.get_by_role("button", name="Submit").click()
        page.wait_for_url("**/web/part/*/details", timeout=10000)
        logger.action()
        logger.entity_created("part", identifier=part_name)

        return page.url.split("/part/")[1].split("/")[0]


def _adjust_stock(page: Page, logger: ScenarioLogger, iteration: int, part_id: str) -> None:
    with logger.step("adjust_stock", iteration=iteration):
        page.goto(f"{BASE_URL}/web/part/{part_id}/stock")
        logger.action()

        page.get_by_label("action-button-add-stock-item").click()
        logger.action()

        page.get_by_label("number-field-quantity").fill(str(_STOCK_QUANTITY))
        with expect_write(page, "/api/stock/"):
            page.get_by_role("button", name="Submit").click()
        expect_dialog_closed(page)
        logger.action()
        logger.entity_created("stock_item", identifier=f"{part_id}:{_STOCK_QUANTITY}")


def _browse_catalog(page: Page, logger: ScenarioLogger, iteration: int) -> None:
    """Eight read interactions over the seeded catalog, and nothing that writes.

    Sized as a ratio rather than as a duration: eight reads to the two writes above, because the
    versions differ in per-HTTP-request server work, so the differential scales with requests
    issued and not with wall clock.

    Deliberately identical in every iteration. A cache only pays on a second read, so a
    scenario that reads almost nothing twice gets little signal from a caching difference.
    Repeating exactly these eight views is what gives a cached version a warm path to converge
    onto and an uncached one nothing, which is the effect the per-iteration figure exists to
    show. Any variation introduced here weakens that.
    """
    with logger.step("browse_catalog", iteration=iteration):
        # 1. The full parts list.
        with expect_refresh(page, "/api/part/?"):
            page.goto(f"{BASE_URL}/web/part/category/index/parts")
        logger.action()

        # 2. A search across it, which is the heaviest query the catalog supports.
        with expect_refresh(page, f"search={_CATALOG_SEARCH}"):
            page.get_by_placeholder("Search").first.fill(_CATALOG_SEARCH)
        logger.action()

        # 3. The category tree.
        with expect_refresh(page, "/api/part/category/?"):
            page.get_by_label("panel-tabs-partcategory").get_by_role(
                "tab", name="Part Categories"
            ).click()
        logger.action()

        # 4. and 5. Drill down two levels. Rows are not links in this table, so a cell is what
        # gets clicked; the URL becomes /web/part/category/<id>/subcategories.
        with expect_refresh(page, "/api/part/category/?parent="):
            page.get_by_role("cell", name=_CATALOG_ROOT, exact=True).first.click()
        logger.action()

        with expect_refresh(page, "/api/part/category/?parent="):
            page.get_by_role("cell", name=_CATALOG_CATEGORY, exact=True).first.click()
        logger.action()

        # 6. That category's own parts, 50 of them over two pages.
        # `/api/part/?category=` and not `?category`, which would also match the
        # `category_detail=true` of interaction 1.
        with expect_refresh(page, "/api/part/?category="):
            page.get_by_label("panel-tabs-partcategory").get_by_role("tab", name="Parts").click()
        logger.action()

        # 7. Page two, which is a fresh query rather than a client-side slice.
        with expect_refresh(page, "offset=25"):
            page.get_by_role("button", name="2", exact=True).first.click()
        logger.action()

        # 8. One part's detail page, the heaviest single interaction in the block at six API
        # calls. Row 1 of page 2 is the same part every iteration, by the table's own ordering.
        with expect_refresh(page, "path_detail=true"):
            page.locator("tbody tr").first.click()
        expect(page).to_have_url(_PART_DETAIL_PATTERN)
        logger.action()


def _run_build_order(page: Page, logger: ScenarioLogger) -> None:
    """Create, issue, allocate, produce and complete one build order.

    Once per repetition rather than once per iteration: it is the one step that looks like real
    ERP work, but its own noise floor doesn't shrink the way per-request work does, so repeating
    it would triple the noise it contributes for little extra signal.
    """
    with logger.step("run_build_order"):
        page.goto(f"{BASE_URL}/web/manufacturing/index/buildorders/")
        logger.action()

        page.get_by_label("action-button-add-build-order").click()
        choose_related_option(page, "related-field-part", "Core Flow Assembly")
        logger.action()
        page.get_by_role("button", name="Submit").click()
        page.wait_for_url("**/build-order/*/details", timeout=10000)
        logger.action()
        logger.entity_created("build_order")

        page.get_by_role("button", name="Issue Order").click()
        with expect_write(page, "/issue/"):
            page.get_by_role("button", name="Submit").click()
        expect_dialog_closed(page)
        logger.action()

        page.get_by_text("Required Parts", exact=True).click()
        page.get_by_label("action-button-auto-allocate-stock").click()
        # The allocation itself runs as a background task, so the build-line re-read is the
        # signal that it happened; the POST only queues it and answers in about 50 ms.
        with expect_refresh(page, "/api/build/line/"), expect_write(page, "/auto-allocate/"):
            page.get_by_role("button", name="Submit").click()
        expect_dialog_closed(page)
        logger.action()

        page.get_by_text("Incomplete Outputs", exact=True).click()
        page.get_by_label("action-button-add-build-output").click()
        with expect_write(page, "/create-output/"):
            page.get_by_role("button", name="Submit").click()
        expect_dialog_closed(page)
        logger.action()

        page.get_by_label("Select record 1").click()
        page.get_by_label("action-button-complete-selected-outputs").click()
        choose_related_option(page, "related-field-location", "Core Flow Output")
        # Completing an output is this step's second background task, same shape as above.
        with expect_refresh(page, "/api/build/"), expect_write(page, "/complete/"):
            page.get_by_role("button", name="Submit").click()
        expect_dialog_closed(page)
        logger.action()

        page.get_by_role("button", name="Complete Order").click()
        # Last action of the last step, so this wait also closes the measured window: waiting
        # for the build to be re-read keeps the app's own response to the finish inside the
        # window rather than just outside it.
        with expect_refresh(page, "/api/build/"), expect_write(page, "/finish/"):
            page.get_by_role("button", name="Submit").click()
        expect_dialog_closed(page)
        logger.action()


@click.command()
@click.option(
    "--output",
    "output_path",
    type=click.Path(path_type=Path),
    required=True,
    help="Where to write the step log (supplied by the orchestrator per repetition).",
)
@click.option("--headed", is_flag=True, help="Run with a visible browser (debugging only).")
def main(output_path: Path, headed: bool) -> None:
    logger = ScenarioLogger(STEPS, output_path)
    run_id = datetime.now().isoformat()

    with browser_page(headless=not headed) as page:
        login(page, BASE_URL)
        for iteration in range(1, ITERATIONS + 1):
            part_id = _create_part(
                page, logger, iteration, part_name=f"Session Part {iteration} {run_id}"
            )
            _adjust_stock(page, logger, iteration, part_id=part_id)
            _browse_catalog(page, logger, iteration)
        _run_build_order(page, logger)

    logger.finalize()


if __name__ == "__main__":
    main()
