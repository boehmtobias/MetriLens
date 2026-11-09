# Adding a Scenario

A scenario is the version-independent description of the workload MetriLens measures: a fixed list of
logical steps, realized by a separate Playwright script per application version being compared. This
doc walks through everything a new scenario needs. See
[scenarios/inventree_session_flow](../../scenarios/inventree_session_flow/) for a complete worked example.

Everything below applies to any target application:

---

## 1. `scenario.yaml`: the step list

```yaml
name: My Scenario
description: >
  What this scenario exercises and why.
steps:
  - create_part
  - adjust_stock
  - browse_catalog
iterations: 1
```

- `steps`: the ordered vocabulary of logical step ids. Every per-version script must realize exactly
  this set, as this is used for equivalence validation (see `iterations` below).
- `iterations` (optional, default 1): how many times a *repeated block* of steps runs inside one
  measured repetition. Leave at 1 unless you have a specific reason to repeat part of the scenario
  (see Section 5).

Load it into the database:

```bash
uv run metrilens scenario load path/to/scenario.yaml
```

## 2. One Playwright script per `app_version`

Each version being compared gets its own script realizing the scenario's steps against that version's
actual UI. The orchestrator runs each script as a subprocess, `python <script_path> --output
<step-log path>`, and treats a non-zero exit as a failed repetition. The script opens its own
browser, logs in, wraps each declared step in `logger.step(...)`, and writes the step log with
`logger.finalize()`:

```python
from pathlib import Path

import click
from playwright_support import browser_page, login  # helper colocated with your scenario

from metrilens.scenario_logger import ScenarioLogger

BASE_URL = "http://localhost:8000"
STEPS = ["create_part", "adjust_stock"]  # the worked example reads these from scenario.yaml


@click.command()
@click.option("--output", "output_path", type=click.Path(path_type=Path), required=True)
def main(output_path: Path) -> None:
    logger = ScenarioLogger(STEPS, output_path)
    with browser_page(headless=True) as page:
        login(page, BASE_URL)  # before the first step, so outside the measured window
        with logger.step("create_part"):
            # ... Playwright actions against this version's UI ...
            logger.action()  # count a UI action, useful for equivalence review
            logger.entity_created("part", identifier="WIDGET-1")  # domain entity, explicit
        with logger.step("adjust_stock"):
            ...
    logger.finalize()


if __name__ == "__main__":
    main()
```

A few rules that matter:

- If one version needs an extra confirmation dialog or an extra click to accomplish the same logical
  step, that's an `action()` inside the existing `step()` block, not a new step id. The scenario's
  step list is version-independent by design, so if a version genuinely needs a distinct extra step,
  that requires a scenario redefinition applying to *both* versions.
- `logger.entity_created(...)` records what got created, since the
  helper can't infer domain meaning from clicks alone. Use it for anything the equivalence check
  should verify happened on both sides (a part created, a record saved).
- Login and credentials go through a `playwright_support` module living beside your scenario's own
  scripts (each scenario folder has its own, since credential env-var names and login flow are
  specific to the target app). This keeps measurement overhead identical across every version's
  script within a scenario, which matters for a fair comparison. Credentials are never hardcoded,
  only the names of the environment variables holding them (see `credentials.py` and
  `.env.example`). If your target app is InvenTree, reuse
  `scenarios/inventree_session_flow/playwright_support.py` rather than writing a new one.
- The script doesn't decide where its own step log is written. The orchestrator supplies the
  output path per repetition; your script just calls `logger.finalize()` at the end.

## 3. `app_versions.yaml`: registering each version

```yaml
versions:
  - label: My App v1 (baseline)
    description: The baseline configuration.
    script_path: scenarios/my_scenario/version_a.py
    reset_script: scenarios/my_scenario/reset_version_a.sh
    compose_file: my-app-baseline/docker-compose.yml
    health_url: http://localhost:8000/api/
    version_ref: my-app 1.0, config A

  - label: My App v2 (modified)
    ...
```

| Field | Purpose                                                                              |
|---|--------------------------------------------------------------------------------------|
| `label` | Human-readable identity. This is the **key** the loader matches against already-loaded versions; a match is skipped, not updated (see below). |
| `script_path` | Path to that version's Playwright script, relative to the repo root.                 |
| `reset_script` | Script that seeds this version after MetriLens's `down -v`/`up -d` (see Section 4).  |
| `compose_file` | This version's `docker-compose.yml`, relative to `stacks_dir` in `config.yaml`.      |
| `health_url` | The HTTP endpoint the version's script actually uses; polled by the readiness check. |
| `version_ref` | Free-text provenance (image tag, commit, config identifier, etc.).                   |

Load it:

```bash
uv run metrilens app-version load path/to/app_versions.yaml --scenario-id <id from step 1>
```

**Insert-only.** Every `repetition` a past session ever measured traces back to an `app_version`
row by foreign key, so re-loading a file after editing an existing entry does **not** update that
row: it's warned about and skipped, so the file stays reloadable as new versions get appended to it.
A changed `script_path`, `compose_file`, or anything else about what a version actually measures is a
**different configuration**, and belongs under a new `label` (edits to an existing one not sufficient).
If you need to change what a version does, add a new entry.

## 4. `reset_script`: reseed after teardown

Before *every* repetition MetriLens runs `docker compose down -v` and `up -d` on the version's
compose file, then that version's `reset_script` from the compose file's directory. The script
brings the stack to a known, reproducible seed state: waiting for migrations, loading fixtures,
recreating whatever admin/login state your scenario's script needs. `down -v` removes named
volumes but not host bind mounts, so if your stack keeps data on a bind mount, the script must
clear it too (or switch the stack to named volumes, as the InvenTree example does). Your scenario
needs to start from identical state every time or repetitions aren't comparable to each other.

## 5. `iterations`: repeating part of the scenario (optional)

Some steps benefit from running several times inside one measured repetition rather than once. Two
main reasons why this can be worth doing:

- A cache (or anything else that only helps on a second hit) can only show up in the numbers if
  something reads the same thing twice. A scenario that never repeats a read gets zero signal from
  caching, no matter how much the cache configuration actually matters in real use.
- Variance may cancel out when the repeated work is genuinely asynchronous (a background job queue,
  for example): per-iteration energy readings inside one repetition can be negatively correlated,
  because the queue redistributes a roughly conserved amount of work in time rather than changing how
  much of it there is, so summing over several iterations cancels out what a single iteration
  is fully exposed to. This is separate from repetition count (30 repetitions per version is the
  general recommendation): iterations aggregate inside one measured window, repetitions are separated
  by a full teardown and reseed.

**If you use it:** put the repeated steps inside `logger.step(step_id, iteration=n)` (1 by default), leave
anything outside the repeated block with no `iteration` argument, and set `scenario.yaml`'s
`iterations` to match. It's a property of the scenario and identical in every version's script.

## 6. Designing steps that can actually express the difference you're testing for

A scenario that "runs the app" isn't automatically a scenario that can detect the specific difference
you're measuring for. A few design rules, learned from decomposing where a scenario's energy actually
goes:

1. **Add requests, not minutes:** A second spent on genuine server interaction (a page load, a form
   submit) is far more likely to carry your differentiating signal than a second spent waiting.
2. **A fixed `sleep`/`wait_for_timeout` is pure dilution:** It adds energy and variance to every
   version equally and signal to none of them. If a step is mostly the harness waiting rather than the
   app working, that step is diluting your measurement, not contributing to it.
3. **Repeat views:** so anything state-dependent (a cache, a warm connection pool) gets a chance to
   matter (see `iterations` above).
4. **Background/asynchronous work (a queue, a batch job) tends to be "deaf" to per-request
   differences:** If your differentiating factor is something that happens per HTTP request (debug
   logging, a cache check, permission lookups), a step dominated by backend processing rather than
   request/response traffic carries a lot of energy and very little of your signal. It inflates
   variance far more than it contributes to detecting the effect. Weight your scenario toward
   read/write interaction accordingly.
5. **Size the read-to-write balance deliberately:** as a ratio rather than as a duration. A
   read-heavy mix (most real usage patterns) exercises more of what typically differs between
   implementation choices than a write-heavy one.

None of this is specific to any one target application; it applies to designing any scenario meant
to detect a specific configuration or implementation difference.
