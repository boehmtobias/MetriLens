# InvenTree stack definitions

The target app's Compose stacks, tracked because `app_version.compose_file` records a **path**
and not content. That loader is deliberately insert-only, since `version_run.app_version_id` is
a foreign key every repetition traces back to, so a changed compose file is a new row under a
new label.

The `.env` examples matter most of all: `INVENTREE_DEBUG` and `INVENTREE_CACHE_ENABLED` are the
study's independent variables (`INVENTREE_TAG` is pinned to the same build in every file), and
diffing an arm's file against `baseline.env.example` is the most reviewable statement of what
is actually being compared.

## Contents

| File | What it is                                                             |
|---|---|
| `docker-compose.yml` | All four stacks, one copy. Byte-identical between them by design. |
| `Caddyfile` | Likewise, one copy for all four. |
| `baseline.env.example` | The baseline arm: `DEBUG=False`, cache enabled, ports 8180/8443. |
| `modified.env.example` | The stacked arm: `DEBUG=True`, cache disabled, ports 8480/8743. |
| `debug-true.env.example` | Single-factor arm: `DEBUG=True`, cache **enabled**, ports 8280/8543. |
| `cache-off.env.example` | Single-factor arm: `DEBUG=False`, cache **disabled**, ports 8380/8643. |

**Four arms, two factors.** `baseline.env.example` and `modified.env.example` move both factors
together, so their result is a joint effect with neither assignable on its own. The two
single-factor files each differ from `baseline.env.example` in **exactly one** measured
variable: diff against the baseline and confirm the only lines that move are the project name,
the site URL, the two host ports, and one of `INVENTREE_DEBUG` / `INVENTREE_CACHE_ENABLED`.

**One copy of the compose file and the Caddyfile, not one per stack.** They are identical
between the arms, and a second copy could drift; keeping one makes that structural rather than
something a test has to assert. Deployment copies it into every stack directory, which the
machine layout requires because Compose resolves `./Caddyfile` relative to the compose file.

## Deploying

Per machine, into the directory named by `stacks_dir` in `config.yaml`:

```
stacks_dir/inventree-baseline/{docker-compose.yml,Caddyfile,.env}
stacks_dir/inventree-modified/{docker-compose.yml,Caddyfile,.env}
stacks_dir/inventree-debug-true/{docker-compose.yml,Caddyfile,.env}
stacks_dir/inventree-cache-off/{docker-compose.yml,Caddyfile,.env}
```

**Only the two stacks a session actually measures need to be up.** A single-factor session runs
`inventree-baseline` against one of the other stacks, keeping exactly two stacks running at a
time so their absolute energies stay comparable across sessions. Bringing all four up would
change the idle background for every arm and cost that comparability for nothing, since no
session measures more than two.

Copy the compose file and Caddyfile into each directory, then create each `.env` from the matching
example and replace `__ADMIN_PASSWORD__` and `__DB_PASSWORD__`. The admin password must be
identical in every stack **and** in MetriLens's own `.env`, since that is what the readiness
login and the Playwright scripts authenticate with, and each reset script recreates the
superuser from the stack's environment before every repetition. `.env` files are 0600 and owned
by the measurement account; nothing here is.

## What the deployed copies and these are allowed to do

They can drift, and that is a weaker failure than a root-executed copy drifting elsewhere in
this project: there, a stale copy means root running old validation logic with no symptom. Here
`docker compose` runs the deployed file and these are the record, so drift makes the record
wrong rather than the machine unsafe. Deliberately not hash-checked at pre-flight for that
reason; the smoke test would still catch a stack that does not work at all.
