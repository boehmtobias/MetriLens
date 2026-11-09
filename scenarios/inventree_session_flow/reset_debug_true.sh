#!/bin/sh
# app_version.reset_script for the session flow scenario, DEBUG=True (cache enabled) app version.
#
# Runs after MetriLens's `docker compose down -v` and `up -d`, with the working directory set to
# this version's compose directory. Waits for migrations to finish, refuses to seed a database
# that is not empty, then seeds the build-order fixtures and the browse catalog that
# `browse_catalog` reads. The four reset scripts differ only in this header and the closing echo;
# each version keeps its own copy because loaded app_version rows reference these paths.
#
# Migration readiness is polled via `showmigrations --plan` rather than `invoke static` or a
# basic query, since a table can already be queryable while a later migration on it is still
# pending. The `docker compose exec` call's own exit status has to gate the loop too, not just
# the migration count parsed from its output: the exec itself can fail right after `up -d`
# with empty stdout, which a bare grep count would misread as zero unapplied migrations.
set -eu

echo "waiting for InvenTree migrations to complete..."
MIGRATION_CHECK_LOG=$(mktemp)
trap 'rm -f "$MIGRATION_CHECK_LOG"' EXIT
attempt=0
until docker compose exec -T inventree-server sh -c \
    'cd /home/inventree/src/backend/InvenTree && python3 manage.py showmigrations --plan' \
    >"$MIGRATION_CHECK_LOG" 2>&1 \
    && [ "$(grep -c '^\[ \]' "$MIGRATION_CHECK_LOG")" = "0" ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 24 ]; then
        echo "InvenTree did not become ready within 2 minutes" >&2
        cat "$MIGRATION_CHECK_LOG" >&2
        exit 1
    fi
    sleep 5
done

docker compose exec -T inventree-server invoke static

docker compose exec -T inventree-server sh -c 'cd /home/inventree/src/backend/InvenTree && python3 manage.py shell' <<'PYEOF'
import time

from django.db import transaction
from part.models import BomItem, Part, PartCategory
from stock.models import StockItem, StockLocation

existing = Part.objects.count() + PartCategory.objects.count() + StockLocation.objects.count()
if existing:
    raise SystemExit(
        f"refusing to seed: database is not empty ({existing} pre-existing part/category/"
        "location rows). 'docker compose down -v' did not wipe this stack's data, so the "
        "reset is not resetting anything. Check that the compose file declares "
        "compose-managed named volumes rather than external volumes or bind mounts."
    )

started = time.time()

# ---- The build-order fixtures ---------------------------------------------------------

StockLocation.objects.get_or_create(
    name="Core Flow Output",
    defaults={"description": "Default location for completed build outputs"},
)

category, _ = PartCategory.objects.get_or_create(
    name="MetriLens Fixtures",
    defaults={"description": "Seed data for the InvenTree session flow scenario"},
)

Part.objects.get_or_create(
    name="Core Flow Component",
    category=category,
    defaults={"description": "Component consumed by the build order", "purchaseable": True},
)
Part.objects.get_or_create(
    name="Core Flow Assembly",
    category=category,
    defaults={"description": "Assembly built during the scenario", "assembly": True},
)

component = Part.objects.get(name="Core Flow Component")
assembly = Part.objects.get(name="Core Flow Assembly")

BomItem.objects.get_or_create(part=assembly, sub_part=component, defaults={"quantity": 1})

if not StockItem.objects.filter(part=component).exists():
    StockItem.objects.create(part=component, quantity=1000)

# ---- The browse catalog ---------------------------------------------------------------

CATALOG_CATEGORIES = [
    ("Catalog Resistors", "Passive resistive components"),
    ("Catalog Capacitors", "Passive capacitive components"),
    ("Catalog Connectors", "Board and panel connectors"),
    ("Catalog Fasteners", "Screws, nuts and washers"),
    ("Catalog Modules", "Pre-assembled functional modules"),
]
PARTS_PER_CATEGORY = 50

store, _ = StockLocation.objects.get_or_create(
    name="Catalog Store",
    defaults={"description": "Holds stock for the seeded browse catalog"},
)
catalog_root, _ = PartCategory.objects.get_or_create(
    name="MetriLens Catalog",
    defaults={"description": "Seeded catalog browsed by the session flow scenario"},
)

created_parts = 0
# One transaction for the whole catalog: 500 autocommits would dominate the runtime.
with transaction.atomic():
    for cat_name, cat_description in CATALOG_CATEGORIES:
        catalog_category, _ = PartCategory.objects.get_or_create(
            name=cat_name, parent=catalog_root, defaults={"description": cat_description}
        )
        ipn_prefix = cat_name.split()[1][:3].upper()
        for index in range(1, PARTS_PER_CATEGORY + 1):
            part, made = Part.objects.get_or_create(
                name=f"{cat_name} {index:03d}",
                category=catalog_category,
                defaults={
                    "description": f"{cat_description}, catalog item {index:03d}",
                    "IPN": f"{ipn_prefix}-{index:03d}",
                    "purchaseable": True,
                    "active": True,
                },
            )
            created_parts += int(made)
            StockItem.objects.get_or_create(
                part=part, location=store, defaults={"quantity": 10 + index}
            )

print(f"seed complete: {created_parts} catalog parts in {time.time() - started:.1f} s")
PYEOF

echo "reset_debug_true.sh complete"
