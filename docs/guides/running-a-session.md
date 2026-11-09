# Running a Measurement Session

Once you have a scenario and its app versions loaded (see [installation.md](installation.md) and
[adding-a-scenario.md](adding-a-scenario.md)) and the machine is in its measurement state (see
[machine-setup.md](machine-setup.md)), this covers how to actually run a measurement session. Run every
command from the repository root.

---

## 1. Start the session

```bash
uv run metrilens run-session --scenario-id <id> --version-a-id <id> --version-b-id <id> --repetitions 30
```

`--version-a-id` matters beyond identifying which two versions to compare: it becomes the **baseline**
every difference in the report is measured relative to. Swapping the two ids inverts the sign of
every signed number in the output, so **pick the side you consider "before" deliberately**.

## 2. What the session does, in order

1. **Pre-flight dry run and equivalence gate:** Each version's full pipeline (teardown, reseed,
   readiness wait, script) runs once, unmeasured. This both smoke-tests the whole mechanism before any
   real measurement time is spent, and produces the step logs the equivalence checker needs. If the
   two versions' step/action/entity counts don't line up, the session halts here with a `FLAGGED`
   verdict rather than burning your whole repetition budget on a workload that turns out not to be
   comparable. Step counts and entity counts must match exactly; action counts (clicks, etc.) are
   recorded but not enforced, since two UIs can legitimately need a different number of actions for
   the same logical step.
2. **Settle, then calibrate:** The machine gets a moment to return to idle after the dry runs (which
   put real load on it), then METRION's idle-power calibration runs fresh (this is what
   `machine-setup.md`'s freeze/thaw behavior is about); expect the machine to look briefly unresponsive
   here. Both versions in this session share this one calibration.
3. **The repetition loop:** `2 * --repetitions` real repetitions run in a randomized order (recorded
   as `execution_order` so you can verify the interleaving afterward, optionally seeded). Each one is a
   full teardown, reseed, readiness wait, measured script run, and write to MetriLens' own database.

**A session can run for hours** depending on repetition count and target-app reset cost; launch it as a
systemd unit, not from an interactive shell (see [machine-setup.md](machine-setup.md)).

## 3. If a repetition fails

The orchestrator auto-retries a failing repetition up to `max_retries` (default 3, configurable in
`config.yaml`) times against the same repetition index before giving up and moving on to the next one;
a session doesn't halt on one bad repetition. A repetition left in its terminal failed state after
exhausting its retries can be retried manually once you've diagnosed and fixed whatever caused it:

```bash
uv run metrilens retry --repetition-id <id>
```

## 4. Generate the report

Once a session's repetitions are done:

```bash
uv run metrilens report --scenario-id <id> --comparison-session-id <id>
```

Writes `comparison_report.md` (setup, per-version statistics, the comparison of the two versions,
per-container energy and supplementary figures), `summary.json` (the same numbers plus one record
per repetition, structured), and every figure (PNG + PDF) to
`evaluation/<host>/<scenario-id>_<comparison-session-id>/`.

## 5. Recording a scenario run (optional, not a measurement)

To produce an illustrative video of a scenario running against one or two versions (for a
presentation, a demo, or just to see what the automated script actually does) use `record` instead
of `run-session`:

```bash
uv run metrilens record --scenario-id <id> --version-a-id <id> [--version-b-id <id>]
```

This is deliberately **not** a measurement: no calibration, no METRION, no database rows. Output goes to
`data/raw/recordings/<stamp>/<label>/` as a WebM, its step log, and a quarter-speed companion option (`--slowdown`).

---

`--help` on any command lists its full options (config/data-dir overrides, the random seed, etc.).
