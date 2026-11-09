"""The `inventree_session_flow` scenario's definition files, reset scripts and version scripts.

`inventree_session_flow` is the repository's only scenario: it declares `iterations` and a
`browse_catalog` step, and runs its block k times.
"""

import ast
import importlib.util
import re
import subprocess
from pathlib import Path

import playwright_support
import pytest
import yaml

SCENARIO_DIR = Path(__file__).resolve().parents[1] / "scenarios" / "inventree_session_flow"

# Four arms: the baseline, the stacked modified version, and the two single-factor variants that
# make `DEBUG` and the cache separately attributable.
_RESET_SCRIPTS = [
    "reset_debug_false.sh",
    "reset_debug_true_cache_off.sh",
    "reset_debug_true.sh",
    "reset_cache_off.sh",
]
_VERSION_SCRIPTS = [
    "debug_false.py",
    "debug_true_cache_off.py",
    "debug_true.py",
    "cache_off.py",
]


@pytest.fixture(scope="module")
def scenario_definition():
    return yaml.safe_load((SCENARIO_DIR / "scenario.yaml").read_text())


@pytest.fixture(scope="module")
def app_versions_definition():
    return yaml.safe_load((SCENARIO_DIR / "app_versions.yaml").read_text())


class TestScenarioYaml:
    def test_has_required_fields(self, scenario_definition):
        assert scenario_definition["name"]
        assert scenario_definition["steps"]

    def test_declares_the_four_step_ids(self, scenario_definition):
        """Four ids, not ten. A k=3 repetition logs 10 steps against these four, since the
        declared list is the vocabulary a step log is checked against rather than its expected
        contents, and keeping it independent of k is what lets a report aggregate by step id
        across iterations."""
        assert scenario_definition["steps"] == [
            "create_part",
            "adjust_stock",
            "browse_catalog",
            "run_build_order",
        ]

    def test_declares_k_as_part_of_the_scenario(self, scenario_definition):
        """A headline number means energy per scenario execution *of k iterations*, so k is a
        property of the definition rather than of a run."""
        assert scenario_definition["iterations"] == 3

    def test_k_is_within_the_ceiling_the_pilot_may_move_it_to(self, scenario_definition):
        """k is fixed from pilot timing: at or under 60 s keeps 3, over drops to 2. Anything
        outside that range means the pilot's decision rule no longer applies."""
        assert scenario_definition["iterations"] in (2, 3)


class TestAppVersionsYaml:
    def test_has_one_entry_per_version_script(self, app_versions_definition):
        script_names = {
            Path(version["script_path"]).name for version in app_versions_definition["versions"]
        }
        assert script_names == set(_VERSION_SCRIPTS)

    @pytest.mark.parametrize(
        "key", ["label", "script_path", "reset_script", "compose_file", "health_url"]
    )
    def test_every_entry_sets_each_required_key(self, app_versions_definition, key):
        assert all(version.get(key) for version in app_versions_definition["versions"])

    @pytest.mark.parametrize("key", ["script_path", "reset_script", "compose_file"])
    def test_paths_are_relative_and_therefore_machine_independent(
        self, app_versions_definition, key
    ):
        """Relative and therefore machine-independent: `script_path`/`reset_script` resolve
        against the repository root and `compose_file` against `stacks_dir` from `config.yaml`,
        which is the one genuinely machine-specific value involved. The `app_version` row still
        stores them absolute."""
        assert all(
            not Path(version[key]).is_absolute() for version in app_versions_definition["versions"]
        )

    def test_no_version_names_a_specific_machine(self, app_versions_definition):
        for version in app_versions_definition["versions"]:
            for key in ("script_path", "reset_script", "compose_file"):
                assert "/home/" not in version[key], (key, version[key])

    @pytest.mark.parametrize("key", ["script_path", "reset_script"])
    def test_repo_relative_paths_resolve_to_real_files(self, app_versions_definition, key):
        """Stronger than the basename check below: these must resolve from the repository root,
        which is what the loader actually does with them."""
        repo_root = Path(__file__).resolve().parent.parent
        for version in app_versions_definition["versions"]:
            assert (repo_root / version[key]).is_file(), version[key]

    def test_labels_are_unique(self, app_versions_definition):
        labels = [version["label"] for version in app_versions_definition["versions"]]
        assert len(set(labels)) == len(labels)

    def test_every_entry_records_the_target_app_build(self, app_versions_definition):
        """The database needs to say which InvenTree build a repetition ran against.

        Asserted as a pattern rather than against a literal build number, so a deliberate upgrade
        does not need a test edit, while what actually matters still fails: an entry with no build
        recorded at all, and the label and `version_ref` drifting onto different builds. Both
        fields carry the build on purpose. `version_ref` is the field the system design
        designates for it, and the **label** is the half that makes a reload take effect, because
        the loader is insert-only and keys on label, so a `version_ref`-only edit would be skipped
        as an existing label and record nothing.
        """
        for version in app_versions_definition["versions"]:
            label_builds = re.findall(r"\d+\.\d+\.\d+", version["label"])
            ref_builds = re.findall(r"\d+\.\d+\.\d+", version["version_ref"])
            assert label_builds, f"label records no build: {version['label']!r}"
            assert ref_builds, f"version_ref records no build: {version['version_ref']!r}"
            assert label_builds == ref_builds, (version["label"], version["version_ref"])

    def test_reset_scripts_exist_in_this_folder(self, app_versions_definition):
        referenced = {
            Path(version["reset_script"]).name for version in app_versions_definition["versions"]
        }
        assert referenced == set(_RESET_SCRIPTS)
        assert all((SCENARIO_DIR / name).exists() for name in referenced)

    def test_it_points_at_its_own_scenario_folder(self, app_versions_definition):
        """Every path names this scenario, which is what a copy-pasted `app_versions.yaml`
        entry from elsewhere would get wrong while still passing every other check here."""
        for version in app_versions_definition["versions"]:
            for key in ("script_path", "reset_script"):
                assert Path(version[key]).parent.name == "inventree_session_flow"

    def test_each_single_factor_arm_moves_exactly_one_stack_away_from_the_baseline(
        self, app_versions_definition
    ):
        """The property the single-factor arms rest on: an arm that differed from the baseline in
        two things would estimate a joint effect again, which is the limitation they exist to
        remove."""
        stacks = {v["label"]: v["compose_file"] for v in app_versions_definition["versions"]}
        baseline = stacks["InvenTree 1.4.2 Session Flow DEBUG=False cache=True"]
        for label in (
            "InvenTree 1.4.2 Session Flow DEBUG=True cache=True",
            "InvenTree 1.4.2 Session Flow DEBUG=False cache=False",
        ):
            assert stacks[label] != baseline
        assert len(set(stacks.values())) == len(stacks)

    def test_versions_point_at_distinct_health_urls(self, app_versions_definition):
        urls = {version["health_url"] for version in app_versions_definition["versions"]}
        assert len(urls) == len(app_versions_definition["versions"])


def _seed_body(script_name: str) -> str:
    """The Python heredoc a reset script feeds to `manage.py shell`."""
    text = (SCENARIO_DIR / script_name).read_text()
    _, _, after = text.partition("<<'PYEOF'\n")
    body, _, _ = after.partition("\nPYEOF")
    return body


class TestResetScripts:
    @pytest.mark.parametrize("script_name", _RESET_SCRIPTS)
    def test_shell_syntax_is_valid(self, script_name):
        result = subprocess.run(
            ["sh", "-n", str(SCENARIO_DIR / script_name)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("script_name", _RESET_SCRIPTS)
    def test_is_executable(self, script_name):
        assert (SCENARIO_DIR / script_name).stat().st_mode & 0o111

    def test_all_versions_seed_byte_identical_data(self):
        """The seed is part of what every arm is measured against, so it may not differ between
        them by so much as a quantity. Every reset script is generated from one body for this
        reason, and this is the assertion that keeps a later hand edit honest."""
        bodies = [_seed_body(name) for name in _RESET_SCRIPTS]
        assert len(set(bodies)) == 1
        assert bodies[0].strip()

    def test_carries_no_machine_specific_path_and_no_sudo(self):
        """No script names its own absolute project directory: `reset_target_app` already runs
        these with the working directory set to the compose file's own directory, so such a path
        would be redundant as well as wrong on a second machine. Named volumes also mean no
        script needs a `sudo rm -rf` to clear external storage. One script suffices:
        `test_all_versions_seed_byte_identical_data` already proves the four are identical."""
        text = (SCENARIO_DIR / "reset_debug_false.sh").read_text()
        for forbidden in ("PROJECT_DIR=", "EXT_VOLUME="):
            assert forbidden not in text, forbidden
        # The only home directory allowed is the one inside the InvenTree container.
        assert set(re.findall(r"/home/[\w.-]+", text)) <= {"/home/inventree"}
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "sudo" not in code

    def test_does_not_tear_the_stack_down_itself(self):
        """No script runs its own teardown cycle. Doing so would double the reset cost of every
        repetition, and the only reason to keep it would be a bind mount `down -v` does not
        touch, which named volumes make moot."""
        text = (SCENARIO_DIR / "reset_debug_false.sh").read_text()
        # Shell lines only: the heredoc body is Python, and its own error message names
        # `docker compose down -v` while explaining why the database should have been empty.
        shell = text.split("<<'PYEOF'")[0]
        code = [line for line in shell.splitlines() if not line.lstrip().startswith("#")]
        assert not any(
            "docker compose down" in line or "docker compose up" in line for line in code
        )

    def test_refuses_to_seed_a_database_that_is_not_empty(self):
        """A freshness guard: without the script's own teardown, a compose file whose volumes
        `down -v` does not actually remove would leave the previous repetition's data in place
        and the seed would quietly succeed against dirty state."""
        text = (SCENARIO_DIR / "reset_debug_false.sh").read_text()
        assert "refusing to seed" in text
        assert "Part.objects.count()" in text

    def test_the_seed_creates_a_catalog_worth_browsing(self):
        """Browsing an empty parts list measures nothing. 250 parts is eleven pages at
        InvenTree's 25-row default; the numbers are asserted because shrinking them is a
        silent way to make `browse_catalog` stop reading anything."""
        body = _seed_body("reset_debug_false.sh")
        assert "PARTS_PER_CATEGORY = 50" in body
        assert body.count('("Catalog ') == 5

    def test_the_seed_still_creates_the_build_order_fixtures(self):
        """`run_build_order` needs its own fixtures seeded: an assembly, a component and a BOM
        linking them."""
        body = _seed_body("reset_debug_false.sh")
        for fixture in ("Core Flow Assembly", "Core Flow Component", "Core Flow Output", "BomItem"):
            assert fixture in body

    def test_the_component_stock_guard_tolerates_a_dirty_instance(self):
        """A completed build output splits the component's stock into several rows, at which
        point `get_or_create(part=component)` raises MultipleObjectsReturned. Unreachable after
        a wipe, but hit at once when the heredoc is run by hand against a running stack."""
        body = _seed_body("reset_debug_false.sh")
        assert "StockItem.objects.filter(part=component).exists()" in body


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCENARIO_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function_nodes(module_name: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse((SCENARIO_DIR / f"{module_name}.py").read_text())
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_")
    }


def _module_without_base_url(script_name: str) -> str:
    tree = ast.parse((SCENARIO_DIR / script_name).read_text())
    kept = []
    for node in tree.body[1:]:  # skip the module docstring
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == "BASE_URL" for t in targets):
            continue
        kept.append(ast.dump(node))
    return "\n".join(kept)


def _action_calls(node: ast.FunctionDef) -> int:
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == "action"
    )


@pytest.fixture(params=_VERSION_SCRIPTS)
def version_module(request, target_app_credentials):
    return _load_module(Path(request.param).stem)


class TestVersionScripts:
    def test_both_scripts_exist(self):
        assert all((SCENARIO_DIR / name).exists() for name in _VERSION_SCRIPTS)

    @pytest.mark.parametrize("module_name", [Path(n).stem for n in _VERSION_SCRIPTS])
    def test_credentials_are_never_embedded(self, module_name):
        """A script never holds a credential value.

        Guards the regression directly: the password once lived in a version script as
        `ADMIN_PASSWORD` and would be committed again the moment someone reintroduced it for
        convenience. A script does not even name the variables; that lives in
        `playwright_support`, along with the login that resolves them.
        """
        module = _load_module(module_name)
        assert not hasattr(module, "ADMIN_USERNAME")
        assert not hasattr(module, "ADMIN_PASSWORD")

    def test_k_and_the_steps_come_from_the_scenario_definition(
        self, version_module, scenario_definition
    ):
        """One source of truth. A script carrying its own copy of k could run a number of
        iterations the `scenario` row does not record, which is the one thing that makes a
        headline number uninterpretable."""
        assert version_module.STEPS == scenario_definition["steps"]
        assert version_module.ITERATIONS == scenario_definition["iterations"]

    def test_the_arms_differ_only_in_base_url(self):
        """Every version script must cost the same to run, so apart from the module docstring
        and `BASE_URL` all four are the same code."""
        bodies = {name: _module_without_base_url(name) for name in _VERSION_SCRIPTS}
        reference = bodies[_VERSION_SCRIPTS[0]]
        for name, body in bodies.items():
            assert body == reference, name

    def test_the_arms_point_at_distinct_stacks(self):
        urls = {_load_module(Path(name).stem).BASE_URL for name in _VERSION_SCRIPTS}
        assert len(urls) == len(_VERSION_SCRIPTS)

    def test_each_arm_matches_its_app_version_health_url(self, app_versions_definition):
        for version in app_versions_definition["versions"]:
            module = _load_module(Path(version["script_path"]).stem)
            assert version["health_url"].startswith(module.BASE_URL)

    @pytest.mark.parametrize("module_name", [Path(n).stem for n in _VERSION_SCRIPTS])
    def test_no_fixed_sleeps_and_no_networkidle(self, module_name):
        """No fixed sleep or network-idle wait anywhere in a version script. A sleep here would
        be worse than a single-pass scenario's, since it would be paid k times."""
        tree = ast.parse((SCENARIO_DIR / f"{module_name}.py").read_text())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not called & {"wait_for_timeout", "wait_for_load_state"}

    def test_the_condition_waits_are_the_shared_ones(self, version_module):
        for name in (
            "choose_related_option",
            "expect_write",
            "expect_refresh",
            "expect_dialog_closed",
            "login",
            "browser_page",
        ):
            assert getattr(version_module, name) is getattr(playwright_support, name)

    _LOGIN_BUDGET_NAMES = [
        "_login",
        "_LOGIN_ATTEMPTS",
        "_LOGIN_RETRY_BACKOFF_S",
        "_LOGIN_CLICK_TIMEOUT_MS",
        "_LOGIN_REDIRECT_TIMEOUT_MS",
        "_FIRST_ROUND_REDIRECT_TIMEOUT_MS",
        "_FIRST_ROUND_BACKOFF_S",
    ]

    @pytest.mark.parametrize("module_name", [Path(n).stem for n in _VERSION_SCRIPTS])
    def test_no_version_carries_its_own_login_budget(self, module_name):
        """Measurement overhead must be identical across every version being
        compared, and the login budget is part of what every repetition pays. What this guards
        is that no script quietly reintroduces a local copy of it; the budget's own behaviour is
        tested in `test_playwright_support.py`."""
        module = _load_module(module_name)
        assert not any(hasattr(module, name) for name in self._LOGIN_BUDGET_NAMES)


class TestBrowserLifecycle:
    """The browser close itself lives in `browser_page` and is tested in
    `test_playwright_support.py`. What these check is the wiring at the call site: that a
    script's `main` really runs its steps inside that context manager, and hands it the
    script's own base URL. Two representative scripts (the baseline and the fully-modified
    arm), not all four: `test_the_arms_differ_only_in_base_url` already proves the call-site
    wiring is byte-identical across every arm, so a wiring bug would show there regardless of
    which arm these two pick.
    """

    @pytest.fixture(params=[_VERSION_SCRIPTS[0], _VERSION_SCRIPTS[1]])
    def version_module(self, request, target_app_credentials):
        return _load_module(Path(request.param).stem)

    @staticmethod
    def _run_until_the_first_step_fails(version_module, monkeypatch, tmp_path):
        monkeypatch.setattr(
            version_module, "_create_part", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        )
        with pytest.raises(RuntimeError, match="x"):
            version_module.main.callback(output_path=tmp_path / "log.json", headed=False)

    def test_the_browser_closes_even_when_a_step_raises(
        self, version_module, monkeypatch, tmp_path, fake_browser, patch_playwright
    ):
        """A step that raises must not skip the close and leak Chromium's temporary profile."""
        patch_playwright(fake_browser)

        self._run_until_the_first_step_fails(version_module, monkeypatch, tmp_path)

        assert fake_browser.closed
        assert not (tmp_path / "log.json").exists()

    def test_the_script_logs_in_against_its_own_base_url(
        self, version_module, monkeypatch, tmp_path, fake_browser, patch_playwright
    ):
        """The one version-specific input the shared helper takes, checked at the call site."""
        patch_playwright(fake_browser)

        self._run_until_the_first_step_fails(version_module, monkeypatch, tmp_path)

        assert fake_browser.pages[0].visited == [f"{version_module.BASE_URL}/web/login"]


class TestBlockComposition:
    """The read-to-write ratio is the sizing rule the measurements produced (rule 5 of
    scenario-energy-sensitivity.md): the versions differ in per-HTTP-request server work, so the
    differential scales with requests issued rather than with wall clock. Asserted on the code
    because it is the design, and because shrinking it is a silent way to weaken the scenario.
    """

    def test_the_browse_block_makes_eight_read_interactions(self):
        assert _action_calls(_function_nodes("debug_false")["_browse_catalog"]) == 8

    def test_the_two_write_steps_are_unchanged_in_shape(self):
        functions = _function_nodes("debug_false")
        assert _action_calls(functions["_create_part"]) == 4
        assert _action_calls(functions["_adjust_stock"]) == 3

    def test_the_browse_block_writes_nothing(self):
        """A write inside the read block would break the ratio and, worse, make iterations
        non-identical, which is what the cache effect depends on."""
        node = _function_nodes("debug_false")["_browse_catalog"]
        source = ast.unparse(node)
        assert "expect_write" not in source
        assert "entity_created" not in source

    def test_a_repetition_logs_the_counts_the_arms_are_gated_on(self, scenario_definition):
        """53 actions and 10 steps at k=3, which is what every arm produces live. The
        equivalence gate compares exactly these counts across arms."""
        functions = _function_nodes("debug_false")
        k = scenario_definition["iterations"]
        per_iteration = sum(
            _action_calls(functions[name])
            for name in ("_create_part", "_adjust_stock", "_browse_catalog")
        )
        assert per_iteration == 15
        assert k * per_iteration + _action_calls(functions["_run_build_order"]) == 53
        assert k * 3 + 1 == 10


class TestMainRunsTheBlockKTimes:
    def test_it_iterates_then_runs_one_build_order(
        self, version_module, monkeypatch, tmp_path, fake_browser, patch_playwright
    ):
        """The structural claim of the whole scenario: k passes through the repeated block, then
        `run_build_order` once. Repeating the build order instead would triple the noise it
        contributes for no extra signal."""
        patch_playwright(fake_browser)
        calls: list[str] = []

        monkeypatch.setattr(
            version_module,
            "_create_part",
            lambda page, logger, iteration, part_name: calls.append(f"create:{iteration}") or "7",
        )
        monkeypatch.setattr(
            version_module,
            "_adjust_stock",
            lambda page, logger, iteration, part_id: calls.append(f"adjust:{iteration}"),
        )
        monkeypatch.setattr(
            version_module,
            "_browse_catalog",
            lambda page, logger, iteration: calls.append(f"browse:{iteration}"),
        )
        monkeypatch.setattr(
            version_module, "_run_build_order", lambda page, logger: calls.append("build")
        )
        monkeypatch.setattr(version_module.ScenarioLogger, "finalize", lambda self: {})

        version_module.main.callback(output_path=tmp_path / "log.json", headed=False)

        assert calls == [
            "create:1",
            "adjust:1",
            "browse:1",
            "create:2",
            "adjust:2",
            "browse:2",
            "create:3",
            "adjust:3",
            "browse:3",
            "build",
        ]

    def test_every_part_it_creates_has_a_distinct_name(self):
        """Iterations share one database, so a repeated name would fail the second create rather
        than measure it. One script suffices: `test_the_arms_differ_only_in_base_url` already
        proves every arm's function bodies are identical."""
        source = ast.unparse(ast.parse((SCENARIO_DIR / "debug_false.py").read_text()))
        assert "Session Part {iteration} {run_id}" in source
