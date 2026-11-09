# Measurement Machine Setup

First, follow [installation.md](installation.md) to install and configure MetriLens. This doc covers the
operational side.

---

## Checklist before a session

1. Set the CPU governor: `sudo cpupower frequency-set -g performance` (not persistent across
   reboots).
2. `uv run python scripts/check_environment.py --mode real` passes (governor, Docker, systemd, RAPL).
   `--mode dev` (the default) skips only the governor check; DRAM is reported informationally in
   both modes and never blocks.
3. The installed `metrion` is built from
   [github.com/boehmtobias/Metrion](https://github.com/boehmtobias/Metrion), not upstream
   `ds-lab/metrion`: `metrion.__file__` and `sys.prefix` resolve inside your root-owned install (see
   [installation.md](installation.md#1-install-metrion)). A rebuild from the wrong source silently
   loses the bugfixes MetriLens depends on.
4. `uv run python scripts/check_deployed_stacks.py` passes, run as the account that owns the
   deployed `.env` files (mode 600).
5. Only the two Docker stacks this session measures are up; everything else is stopped.
6. No interactive session is active besides the one you launch from, your own desktop included
   (`loginctl list-sessions`). Log out of a GUI session and launch over SSH instead.
7. Launch the session as its own systemd unit, not from a terminal (see below).

The sections below explain each step.

---

## Why the machine's steady state isn't its measurement state

A machine's normal, everyday configuration (power-saving CPU governor, whatever else happens to be
logged in, background timers) is deliberately not what a measurement session runs under. Getting
into and back out of the measurement state is a per-session process:

### CPU governor is set

Set the governor to `performance` before a session and restore it to your normal default (commonly
`powersave`) afterward:

```bash
sudo cpupower frequency-set -g performance   # before
sudo cpupower frequency-set -g powersave     # after
```

This does not survive a reboot and shouldn't be left on `performance` permanently.
>`uv run python scripts/check_environment.py --mode real` checks the current governor and fails if it isn't fixed.

### Nobody else is logged in

MetriLens's reproducibility recipe requires a genuinely quiet machine for idle-power calibration
and an uncontaminated measurement window. Any other interactive session, including your own desktop
if you're connected over a GUI protocol, is a real energy confound since it does periodic background
work you can't account for.

> Confirm with `loginctl list-sessions` before starting, and prefer connecting over SSH instead
of a GUI session for launching the run itself (see the freeze/reconnect note below for why this matters specifically).

### METRION is still built from the right source

MetriLens depends on a small number of bugfixes not yet in upstream METRION. Install from
[github.com/boehmtobias/Metrion](https://github.com/boehmtobias/Metrion) rather than upstream.

### Only the containerized stacks this session measures are up

Bring up only the two `app_version` stacks being compared in this session; leave every other target
stack stopped (`docker compose stop`, not `down`; that leaves volumes and networks alone for next
time). Every stack that's up is background load competing for the same idle-power denominator MetriLens
divides energy attribution against, so a stack you aren't measuring in this session shouldn't be
running during it.

---

## Launch a session as its own systemd unit, never from a login shell

METRION's idle-power calibration freezes essentially every `systemd` slice unit on the machine for
the duration of its dwell window, which in practice suspends every login session on the machine.
A run started directly from an interactive shell (even backgrounded with `nohup`/`setsid`) dies with
that session when the freeze hits it or when your connection drops.

Launch it as a transient unit instead, so it survives independently of any login session:

```bash
sudo systemd-run --unit=metrilens-session --slice=user.slice --uid=<user> --gid=<group> \
  --working-directory=/path/to/metrilens --setenv=HOME=/home/<user> \
  --setenv=UV_PROJECT_ENVIRONMENT=/path/to/venv \
  --property=StandardOutput=append:/path/to/metrilens/data/session.log \
  --property=StandardError=append:/path/to/metrilens/data/session.log \
  /path/to/uv run --no-sync metrilens run-session ...
```

Expect to be disconnected during each calibration and expect the machine to look unresponsive for
that window. **Do not reboot it**, reconnect afterward. `systemctl status metrilens-session` and the
log file both survive independently of your connection.

### An RDP (or similar GUI-protocol) session does not survive the freeze cleanly

An SSH connection reconnects fine after a calibration freeze thaws. A GUI session over RDP or a
similar protocol often does not: the front-end service itself typically isn't frozen (it usually
lives outside `user.slice`), but the actual desktop session underneath it is, so the client times out
against a backend that's temporarily unscheduled.

Recovery, without touching the running measurement session (which is a separate systemd unit, not a
login session, so it's unaffected by anything you do here):

```bash
loginctl list-sessions
loginctl terminate-session <the stuck GUI session>
```

Then reconnect for a fresh session. Prevention is simpler: log out of any GUI session and launch over
SSH instead.

### If a session stays frozen well beyond its own window

Rarely, a frozen unit can outlive the calibration that froze it, a freeze/thaw defect fixed in
[boehmtobias/Metrion](https://github.com/boehmtobias/Metrion) but worth knowing how to recognize and
recover from regardless. Check directly:

```bash
cat /sys/fs/cgroup/user.slice/cgroup.freeze
cat /sys/fs/cgroup/user.slice/user-<uid>.slice/cgroup.freeze
```

A `1` here with no session or measurement unit currently running to justify it is this condition.
Recovery is a direct, safe thaw with no data loss. It resumes whatever was suspended exactly where
it left off:

```bash
sudo systemctl thaw user@<uid>.service
```

---

## Checklist for after a session

1. Restore the CPU governor to its normal default.
2. Sweep METRION's own per-run database directory (`metrion_db_dir`). MetriLens copies each run's
   database into its own archive, so METRION's originals accumulate there unbounded. Deleting them
   needs write access to that directory, not root, if it's set up per `config.example.yaml`'s
   recommendation. Confirm your archived copies are intact first.
3. Reset a failed systemd unit before reusing its name: `systemctl reset-failed metrilens-session`.

---

## A note on hardware

DRAM energy attribution needs a DRAM RAPL domain, which only exists on server-class (Xeon-family)
Intel CPUs. A client-class CPU physically has no such counter to expose, regardless of permissions
or configuration. `scripts/check_environment.py` reports this as informational rather than blocking,
since MetriLens degrades gracefully (CPU-only) when it's absent; see
[installation.md](installation.md#0-requirements) for the full prerequisite list.
