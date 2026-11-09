"""The tracked InvenTree stack definitions.

These files are the record of what a session actually measured, so what matters is that the two
arms differ in exactly the intended places and nowhere else. An unintended difference between
them is a confound the pipeline cannot detect and the report would attribute to the treatment.
"""

from pathlib import Path

import pytest
import yaml

STACK_DIR = Path(__file__).resolve().parent.parent / "stacks" / "inventree"

# Everything two arms are allowed to differ in. The first two are the study's independent
# variables; the rest are the bookkeeping that lets the stacks run side by side.
_EXPECTED_DIFFERING_KEYS = {
    "INVENTREE_DEBUG",
    "INVENTREE_CACHE_ENABLED",
    "COMPOSE_PROJECT_NAME",
    "INVENTREE_SITE_URL",
    "INVENTREE_HTTP_PORT",
    "INVENTREE_HTTPS_PORT",
}
_SERVICES = {
    "inventree-db",
    "inventree-cache",
    "inventree-server",
    "inventree-worker",
    "inventree-proxy",
}


def _env(name: str) -> dict[str, str]:
    values = {}
    for line in (STACK_DIR / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value.strip('"')
    return values


@pytest.fixture(scope="module")
def baseline():
    return _env("baseline.env.example")


@pytest.fixture(scope="module")
def modified():
    return _env("modified.env.example")


@pytest.fixture(scope="module")
def debug_true():
    return _env("debug-true.env.example")


@pytest.fixture(scope="module")
def cache_off():
    return _env("cache-off.env.example")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((STACK_DIR / "docker-compose.yml").read_text())


class TestTheTwoArmsDifferOnlyWhereIntended:
    def test_the_same_keys_are_set_on_both_sides(self, baseline, modified):
        assert set(baseline) == set(modified)

    def test_only_the_expected_keys_differ(self, baseline, modified):
        differing = {key for key in baseline if baseline[key] != modified[key]}
        assert differing == _EXPECTED_DIFFERING_KEYS

    def test_the_two_measured_factors_are_stated_explicitly_on_both_sides(self, baseline, modified):
        """Not merely absent-versus-set. workstation1's baseline omitted INVENTREE_DEBUG and relied
        on the image default, so a change to that default would have moved this arm silently."""
        assert baseline["INVENTREE_DEBUG"] == "False"
        assert modified["INVENTREE_DEBUG"] == "True"
        assert baseline["INVENTREE_CACHE_ENABLED"] == "True"
        assert modified["INVENTREE_CACHE_ENABLED"] == "False"

    def test_both_arms_pin_the_same_explicit_inventree_version(self, baseline, modified):
        """`stable` moves, so two stacks pulling it at different times can end up on different
        builds and the session would measure a version difference on top of the two factors."""
        tag = baseline["INVENTREE_TAG"]
        assert tag == modified["INVENTREE_TAG"]
        assert tag not in {"stable", "latest"}

    def test_no_real_credential_is_committed(self, baseline, modified):
        for env in (baseline, modified):
            assert env["INVENTREE_ADMIN_PASSWORD"] == "__ADMIN_PASSWORD__"
            assert env["INVENTREE_DB_PASSWORD"] == "__DB_PASSWORD__"


class TestComposeFile:
    def test_it_declares_every_service_the_pipeline_expects(self, compose):
        """MetriLens keys per-container attribution by *service* name, so these are what the
        report's per-container figures are labelled with."""
        assert set(compose["services"]) == _SERVICES

    def test_no_service_pins_a_container_name(self, compose):
        """Docker names are global, so two stacks both naming `inventree-db` cannot run side by
        side. Omitting it also makes each container say which stack it belongs to."""
        for name, service in compose["services"].items():
            assert "container_name" not in service, name

    def test_every_data_mount_is_a_compose_managed_named_volume(self, compose):
        """The property the whole reset design now rests on: `docker compose down -v` removes
        named volumes but never touches a bind mount, which is why the reset scripts need no root
        and why they may assume they start on an empty database."""
        declared = set(compose["volumes"])
        assert declared, "no named volumes declared"
        for name, service in compose["services"].items():
            for mount in service.get("volumes", []):
                source = mount.split(":")[0]
                if source.startswith((".", "/", "$")):
                    # The only permitted bind mount is the read-only Caddyfile.
                    assert mount.endswith(":ro") and "Caddyfile" in mount, (name, mount)
                else:
                    assert source in declared, (name, mount)

    def test_the_caddyfile_roots_resolve_inside_a_mounted_volume(self, compose):
        """The proxy serves /var/www/static and /var/www/media, which exist only because the
        server's data volume is mounted at /var/www rather than per-subdirectory."""
        targets = [m.split(":")[1] for m in compose["services"]["inventree-proxy"]["volumes"]]
        assert "/var/www" in targets
        caddyfile = (STACK_DIR / "Caddyfile").read_text()
        assert "root * /var/www/static" in caddyfile
        assert "root * /var/www/media" in caddyfile


class TestTheSingleFactorArmsMoveExactlyOneFactor:
    """An arm that moves both factors at once measures a joint effect, and neither factor is
    assignable. These two arms each isolate one factor, and the property
    the whole design rests on is narrow enough to state as a test: each differs from the baseline
    in **one** measured variable and in nothing else except the bookkeeping that lets four stacks
    coexist. An arm that moved both would estimate a joint effect again, silently.
    """

    # The same allowance as the stacked pair, minus the factor being held constant. Written per
    # arm rather than derived, so that a future edit which quietly moves the held factor has to
    # change this list too.
    _DEBUG_ONLY = {
        "INVENTREE_DEBUG",
        "COMPOSE_PROJECT_NAME",
        "INVENTREE_SITE_URL",
        "INVENTREE_HTTP_PORT",
        "INVENTREE_HTTPS_PORT",
    }
    _CACHE_ONLY = {
        "INVENTREE_CACHE_ENABLED",
        "COMPOSE_PROJECT_NAME",
        "INVENTREE_SITE_URL",
        "INVENTREE_HTTP_PORT",
        "INVENTREE_HTTPS_PORT",
    }

    def test_debug_only_moves_debug_and_holds_the_cache(self, baseline, debug_true):
        assert set(baseline) == set(debug_true)
        assert {k for k in baseline if baseline[k] != debug_true[k]} == self._DEBUG_ONLY
        assert debug_true["INVENTREE_DEBUG"] == "True"
        assert (
            debug_true["INVENTREE_CACHE_ENABLED"] == baseline["INVENTREE_CACHE_ENABLED"] == "True"
        )

    def test_cache_only_moves_the_cache_and_holds_debug(self, baseline, cache_off):
        assert set(baseline) == set(cache_off)
        assert {k for k in baseline if baseline[k] != cache_off[k]} == self._CACHE_ONLY
        assert cache_off["INVENTREE_CACHE_ENABLED"] == "False"
        assert cache_off["INVENTREE_DEBUG"] == baseline["INVENTREE_DEBUG"] == "False"

    def test_the_four_arms_span_the_two_by_two(self, baseline, modified, debug_true, cache_off):
        """All four cells present and distinct, which is what makes the interaction estimable at
        all: `joint - DEBUG - cache` needs every one of them."""
        cells = {
            (e["INVENTREE_DEBUG"], e["INVENTREE_CACHE_ENABLED"])
            for e in (baseline, modified, debug_true, cache_off)
        }
        assert cells == {("False", "True"), ("True", "True"), ("False", "False"), ("True", "False")}

    def test_no_two_arms_share_a_port_or_a_project_name(
        self, baseline, modified, debug_true, cache_off
    ):
        """Four stacks can be deployed at once even though a session only ever brings two up, so a
        collision here is a deployment that half-works rather than one that fails loudly."""
        arms = (baseline, modified, debug_true, cache_off)
        for key in ("COMPOSE_PROJECT_NAME", "INVENTREE_HTTP_PORT", "INVENTREE_HTTPS_PORT"):
            assert len({e[key] for e in arms}) == len(arms), key

    def test_the_image_pin_is_identical_across_all_four(
        self, baseline, modified, debug_true, cache_off
    ):
        """A session must not measure an InvenTree version
        difference on top of the factors it is isolating."""
        assert len({e["INVENTREE_TAG"] for e in (baseline, modified, debug_true, cache_off)}) == 1
