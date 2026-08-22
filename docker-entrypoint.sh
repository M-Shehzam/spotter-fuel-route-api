#!/bin/sh
# Bring the database up to date and load the price data before serving.
# Author: Muhammad Shehzam
set -e

echo "==> Waiting for the database"
python - <<'PY'
import os, time
import dj_database_url
from django.db.utils import OperationalError

url = os.getenv("DATABASE_URL", "")
if not url:
    print("    DATABASE_URL unset, using SQLite")
else:
    import django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()
    from django.db import connection
    for attempt in range(30):
        try:
            connection.ensure_connection()
            print("    connected")
            break
        except OperationalError:
            time.sleep(1)
    else:
        raise SystemExit("database never became reachable")
PY

echo "==> Migrating"
python manage.py migrate --noinput

echo "==> Loading truckstop prices"
python manage.py load_stations

echo "==> Warming indexes"
python manage.py warm_indexes

echo "==> Starting"
exec "$@"
