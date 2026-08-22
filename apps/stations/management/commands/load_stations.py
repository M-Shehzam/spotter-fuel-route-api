"""Load the committed geocoded station file into the database.

Needs no network: ``data/stations_geocoded.csv`` ships with the repo, so a
reviewer can go from clone to a populated database in one command.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.stations.models import GeocodePrecision, Station

BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "Load data/stations_geocoded.csv into the Station table"

    def add_arguments(self, parser):
        parser.add_argument(
            "--input",
            type=Path,
            default=settings.STATIONS_GEOCODED_CSV,
            help="Geocoded station file to load.",
        )
        parser.add_argument(
            "--truncate",
            action="store_true",
            help="Delete existing stations before loading.",
        )

    def handle(self, *args, **options):
        source: Path = options["input"]
        if not source.exists():
            raise CommandError(
                f"{source} not found. Generate it first with:\n"
                "  python manage.py build_station_data"
            )

        with source.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        if not rows:
            raise CommandError(f"{source} is empty")

        stations = [self._to_station(row) for row in rows]

        with transaction.atomic():
            if options["truncate"]:
                deleted, _ = Station.objects.all().delete()
                self.stdout.write(f"  removed {deleted:,} existing rows")
            Station.objects.bulk_create(
                stations,
                batch_size=BATCH_SIZE,
                update_conflicts=True,
                update_fields=[
                    "name",
                    "address",
                    "city",
                    "state",
                    "rack_id",
                    "retail_price",
                    "price_sample_count",
                    "price_min",
                    "price_max",
                    "latitude",
                    "longitude",
                    "geocode_precision",
                ],
                unique_fields=["opis_id"],
            )

        total = Station.objects.count()
        geocoded = Station.objects.geocoded().count()
        self.stdout.write(f"  loaded    {total:,} stations")
        self.stdout.write(
            f"  geocoded  {geocoded:,} ({geocoded / total * 100:.2f}%)" if total else ""
        )
        self.stdout.write(self.style.SUCCESS("Stations loaded"))

    @staticmethod
    def _to_station(row: dict[str, str]) -> Station:
        def decimal_or_zero(key: str) -> Decimal:
            value = (row.get(key) or "").strip()
            return Decimal(value) if value else Decimal("0")

        def float_or_none(key: str) -> float | None:
            value = (row.get(key) or "").strip()
            return float(value) if value else None

        def int_or_none(key: str) -> int | None:
            value = (row.get(key) or "").strip()
            return int(value) if value else None

        return Station(
            opis_id=int(row["opis_id"]),
            name=row["name"],
            address=row["address"],
            city=row["city"],
            state=row["state"],
            rack_id=int_or_none("rack_id"),
            retail_price=decimal_or_zero("retail_price"),
            price_sample_count=int(row["price_sample_count"] or 1),
            price_min=decimal_or_zero("price_min"),
            price_max=decimal_or_zero("price_max"),
            latitude=float_or_none("latitude"),
            longitude=float_or_none("longitude"),
            geocode_precision=row.get("geocode_precision") or GeocodePrecision.UNKNOWN,
        )
