# Installation

This guide takes a Linux host from nothing to a loaded scenario. Before each session, follow
[machine-setup.md](machine-setup.md); to run one, see [running-a-session.md](running-a-session.md).

---

## 0. Requirements

- Linux with systemd, on bare metal (no VM).
- An Intel CPU with RAPL. DRAM energy additionally needs a DRAM RAPL domain, which only
  server-class CPUs (for example Xeon) expose; without it MetriLens measures CPU energy only.
- Docker with Compose, Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).
- Passwordless `sudo` for the commands listed in step 1.

A dedicated machine and a dedicated account (the examples below use `metrilens`) are recommended:
a session runs for hours, and every other process on the host adds noise.

`uv run python scripts/check_environment.py --mode real` checks these prerequisites once step 3's
`uv sync` has run.

Run every `metrilens` command and script from the repository root: MetriLens keeps its own database
at `data/metrilens.db` relative to the working directory, so running from elsewhere starts an empty
one.

---

## 1. Install METRION

METRION is a separate project and not vendored here. MetriLens is built and tested against
[github.com/boehmtobias/Metrion](https://github.com/boehmtobias/Metrion), a fork of
[ds-lab/metrion](https://github.com/ds-lab/metrion) carrying a small number of bugfixes that
unattended sessions rely on. Follow that repository's setup instructions to get a working `.venv`
in the cloned repository (it needs a Rust toolchain and [BCC](https://github.com/iovisor/bcc) 0.35+
besides Python 3.12).

A measurement session needs passwordless `sudo`: METRION needs root for eBPF and RAPL, and the
orchestrator calls `metrion monitor` and `analyze` (plus `pkill`, `rm` and a cleanup script)
non-interactively mid-session, where a password prompt would hang the run.

The `sudo` grant must not point at the cloned venv, since the owning account can write to it. From
inside the METRION clone, copy it to a root-owned location:

```bash
sudo install -d -o root -g root -m 755 /opt/metrion-runtime
sudo cp -r .venv /opt/metrion-runtime/venv
sudo chown -R root:root /opt/metrion-runtime/venv
sudo chmod -R go-w /opt/metrion-runtime/venv
```

A plain copy is not enough: an editable install leaves a `.pth` file pointing back at the source
tree, and console scripts carry an absolute interpreter path. Correct both:

```bash
# rewrite the binary's shebang to the new venv's interpreter
sudo sed -i '1s|.*|#!/opt/metrion-runtime/venv/bin/python3|' /opt/metrion-runtime/venv/bin/metrion

# replace the editable-install pointer with a real copy of the package
sudo cp -r metrion /opt/metrion-runtime/venv/lib/python3.12/site-packages/
sudo chown -R root:root /opt/metrion-runtime/venv/lib/python3.12/site-packages/metrion
sudo chmod -R go-w /opt/metrion-runtime/venv/lib/python3.12/site-packages/metrion
sudo rm /opt/metrion-runtime/venv/lib/python3.12/site-packages/metrion.pth
```

Check that both resolve inside `/opt/metrion-runtime/venv` and not the clone:

```bash
/opt/metrion-runtime/venv/bin/python3 -c 'import metrion, sys; print(metrion.__file__, sys.prefix)'
```

Two MetriLens scripts also run as root:
[`scripts/metrion_analyze_wrapper.py`](../../scripts/metrion_analyze_wrapper.py) (runs
`metrion analyze` for one repetition) and
[`scripts/cleanup_orphan_batch_files.py`](../../scripts/cleanup_orphan_batch_files.py) (removes
METRION's leftover `/tmp/batch_*.ipc` files). The analyze wrapper validates its own arguments, so it
hardcodes the METRION binary and database directory instead of reading the config. In the MetriLens
checkout (step 3), set `METRION_BINARY` and `DB_ROOT` in the wrapper to your `metrion_binary_path`
and `metrion_db_dir`, then install root-owned copies and point `config.yaml` at them
(`analyze_wrapper_path`, `batch_cleanup_script_path`):

```bash
sudo install -d -o root -g root -m 755 /opt/metrion-runtime/bin
sudo install -o root -g root -m 755 scripts/metrion_analyze_wrapper.py /opt/metrion-runtime/bin/metrion_analyze.py
sudo install -o root -g root -m 755 scripts/cleanup_orphan_batch_files.py /opt/metrion-runtime/bin/cleanup_orphan_batch_files.py
```

Then add `NOPASSWD` entries in `/etc/sudoers.d/metrilens` (mode `0440`), one for each command
MetriLens runs under `sudo`:

```
metrilens ALL=(root) NOPASSWD: /opt/metrion-runtime/venv/bin/metrion monitor -c /path/to/metrion/config.yaml
metrilens ALL=(root) NOPASSWD: /usr/bin/python3 /opt/metrion-runtime/bin/metrion_analyze.py *
metrilens ALL=(root) NOPASSWD: /usr/bin/python3 /opt/metrion-runtime/bin/cleanup_orphan_batch_files.py *
metrilens ALL=(root) NOPASSWD: /usr/bin/pkill -INT -f /opt/metrion-runtime/venv/bin/python3
metrilens ALL=(root) NOPASSWD: /usr/bin/pkill -KILL -f /opt/metrion-runtime/venv/bin/python3
metrilens ALL=(root) NOPASSWD: /usr/bin/pkill -INT -P *
metrilens ALL=(root) NOPASSWD: /usr/bin/pkill -KILL -P *
metrilens ALL=(root) NOPASSWD: /usr/bin/rm -f /path/to/metrion/rapl-idle.log
```

Only the two root-owned scripts and the `pkill -P` child sweep take a trailing wildcard. `analyze`
goes through the wrapper because some sudo implementations reject wildcards inside command
arguments, and its database path and time window change per repetition. `pgrep` needs no grant,
since PID lookups read `/proc` unprivileged. The `rm` path must equal `metrion_idle_log_path`.
Validate the file with `visudo -c`; step 5 checks the grants themselves.

---

## 2. Configure METRION

METRION reads its own `configs/config.yaml`. Enable or disable measured components (`cpu`, `dram`,
`gpu`, `network`) under `energy_model.components`; MetriLens uses `cpu` and, where the hardware has
it, `dram`. Without a DRAM RAPL domain MetriLens reports DRAM as missing (`None`, not `0`).

---

## 3. Install and configure MetriLens

```bash
uv sync
uv run playwright install --with-deps chromium
cp config.example.yaml config.yaml
cp .env.example .env && chmod 600 .env
```

- `playwright install` fetches the browser the scenario scripts drive; `uv sync` only installs the
  Python bindings. `--with-deps` adds the system libraries Chromium needs.
- In `config.yaml`, set the absolute paths: `metrion_binary_path` (the root-owned copy from step 1),
  `metrion_config_path` (step 2), `metrion_output_dir`, `metrion_idle_log_path` and
  `metrion_db_dir` (METRION's export, calibration and database locations), and the two helper
  script paths from step 1. The comments in [`config.example.yaml`](../../config.example.yaml)
  explain each key.
- The `readiness_auth` block is optional but recommended: the readiness check then drives one real
  login before a repetition starts instead of trusting the health endpoint. A freshly reset stack
  can answer its health endpoint before login works, so without it the scenario's own login needs
  more retries and fails a repetition more often. Login happens before the first measured step, so
  this costs time, not measured energy.
- Put the target application's login credentials in `.env`
  (`INVENTREE_ADMIN_USER`/`INVENTREE_ADMIN_PASSWORD` for the worked example). Scenario scripts
  resolve these variables by name at login time. A variable already set in the environment wins
  over the file, so `INVENTREE_ADMIN_PASSWORD=... uv run metrilens run-session ...` overrides it
  for one run.

---

## 4. Deploy the target application and load a scenario

A session compares two Docker Compose stacks (`app_version`s) of the same application. For the
InvenTree example, deploy the stacks as described in
[stacks/inventree/README.md](../../stacks/inventree/README.md), then load the scenario and its
versions:

```bash
uv run metrilens scenario load scenarios/inventree_session_flow/scenario.yaml
uv run metrilens app-version load scenarios/inventree_session_flow/app_versions.yaml --scenario-id <id>
```

`scenario load` inserts a new row every time it runs, so load a scenario once. `app-version load`
is insert-only: a label already on record is skipped with a warning rather than updated, because
every past repetition refers to its app version by id. A changed script or compose file is a new
entry under a new label.

For your own application, [adding-a-scenario.md](adding-a-scenario.md) covers the scenario file,
the per-version Playwright scripts, the reset scripts and `app_versions.yaml`.

---

## 5. Verify the setup

```bash
uv run python scripts/check_environment.py --mode real    # host prerequisites
uv run python scripts/check_run_session_sudo.py           # every sudo grant, and the root-owned script copies
uv run python scripts/check_deployed_stacks.py            # InvenTree stacks; run as the owner of their .env files
uv run python scripts/smoke_test_session.py --version-a-id <id> --version-b-id <id>
```

`check_run_session_sudo.py` also confirms the root-owned script copies still match the checkout
byte for byte, so re-install them after editing either script. The smoke test runs one small real
session (one repetition per version by default) end to end, including idle calibration, and records
it like any other session, so put the machine into its measurement state first.

Next: put the machine into its measurement state with [machine-setup.md](machine-setup.md), then
start a session as described in [running-a-session.md](running-a-session.md).
