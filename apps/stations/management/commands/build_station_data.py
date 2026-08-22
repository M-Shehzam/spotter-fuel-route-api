"""Build the committed, pre-geocoded station file.

Run once by the author; the resulting CSV ships in the repo so that
``load_stations`` needs no network. Requires the GeoNames US dump, which is too
large to commit:

    curl -L -o data/geonames_raw/US.zip https://download.geonames.org/export/dump/US.zip
    unzip -o data/geonames_raw/US.zip -d data/geonames_raw
    python manage.py build_station_data
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.stations.cleaning import clean
from apps.stations.geocoding import CityIndex
from apps.stations.models import GeocodePrecision

FIELDNAMES = [
    "opis_id",
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
]


class Command(BaseCommand):
    help = "Clean the supplied price file, geocode it offline, and write data/stations_geocoded.csv"

    def add_arguments(self, parser):
        parser.add_argument(
            "--geonames",
            type=Path,
            default=settings.BASE_DIR / "data" / "geonames_raw" / "US.txt",
            help="Path to the unpacked GeoNames US dump.",
        )
        parser.add_argument(
            "--source",
            type=Path,
            default=settings.STATIONS_CSV,
            help="The supplied price file to clean.",
        )
        parser.add_argument(
            "--output",
            type=Path,
            default=settings.STATIONS_GEOCODED_CSV,
            help="Where to write the geocoded station file.",
        )

    def handle(self, *args, **options):
        source: Path = options["source"]
        geonames: Path = options["geonames"]
        output: Path = options["output"]

        if not source.exists():
            raise CommandError(f"Price file not found: {source}")
        if not geonames.exists():
            raise CommandError(
                f"GeoNames dump not found: {geonames}\n"
                "Download it with:\n"
                "  curl -L -o data/geonames_raw/US.zip "
                "https://download.geonames.org/export/dump/US.zip\n"
                "  unzip -o data/geonames_raw/US.zip -d data/geonames_raw"
            )

        self.stdout.write(self.style.MIGRATE_HEADING("Cleaning the price file"))
        records, cleaning_report = clean(source)
        for line in cleaning_report.as_lines():
            self.stdout.write(f"  {line}")

        self.stdout.write(self.style.MIGRATE_HEADING("Indexing GeoNames"))
        index = CityIndex.from_dump(geonames)
        self.stdout.write(f"  {len(index):,} places indexed")

        self.stdout.write(self.style.MIGRATE_HEADING("Geocoding"))
        matched = 0
        unmatched: list[str] = []
        output.parent.mkdir(parents=True, exist_ok=True)

        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writeheader()

            for record in records:
                position = index.lookup(record.city, record.state)
                if position is None:
                    unmatched.append(f"{record.city}, {record.state}")
                    latitude = longitude = None
                    precision = GeocodePrecision.UNKNOWN
                else:
                    latitude, longitude = position
                    precision = GeocodePrecision.CITY
                    matched += 1

                writer.writerow(
                    {
                        "opis_id": record.opis_id,
                        "name": record.name,
                        "address": record.address,
                        "city": record.city,
                        "state": record.state,
                        "rack_id": "" if record.rack_id is None else record.rack_id,
                        "retail_price": record.retail_price,
                        "price_sample_count": record.price_sample_count,
                        "price_min": record.price_min,
                        "price_max": record.price_max,
                        "latitude": "" if latitude is None else f"{latitude:.6f}",
                        "longitude": "" if longitude is None else f"{longitude:.6f}",
                        "geocode_precision": precision.value,
                    }
                )

        coverage = matched / len(records) * 100 if records else 0.0
        self.stdout.write(f"  geocoded {matched:,}/{len(records):,} ({coverage:.2f}%)")
        if unmatched:
            self.stdout.write(
                self.style.WARNING(f"  {len(unmatched)} unmatched: {', '.join(unmatched[:10])}")
            )

        self.stdout.write(self.style.SUCCESS(f"Wrote {output}"))
