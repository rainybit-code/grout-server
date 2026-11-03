#!/bin/sh
# Applies the re-runnable database scripts (db/init/05_* and later) to an existing database.
# Fresh databases get every db/init script automatically on first start; this is for upgrades.
set -eu
cd "$(dirname "$0")/.."
for f in db/init/*.sql; do
  n=$(basename "$f" | cut -c1-2)
  [ "$n" -ge 5 ] 2>/dev/null || continue
  echo "applying $f"
  docker compose exec -T -e PGOPTIONS="-c client_min_messages=warning" db psql -q -U habits -d habits -v ON_ERROR_STOP=1 < "$f"
done
echo "database is up to date"
