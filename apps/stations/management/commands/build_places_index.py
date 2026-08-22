"""Build the committed place index used to resolve user input.

The API accepts "Dallas, TX" rather than raw coordinates, and resolving that
must not cost an external call: the brief asks for one call per request, and
that one is the route itself. So a compact gazetteer ships with the repo.

Run once by the author, alongside build_station_data:

    python manage.py build_places_index
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.stations.geocoding import (
    CityIndex,
    normalize_city,
    COL_ADMIN1,
    COL_ALTERNATES,
    COL_ASCII,
    COL_FEATURE_CLASS,
    COL_FEATURE_CODE,
    COL_LAT,
    COL_LON,
    COL_NAME,
    COL_POPULATION,
    HISTORICAL_CODES,
    POPULATED_CLASS,
)

FIELDNAMES = ["name", "state", "latitude", "longitude", "population"]

# Everything at least this large is included, so any city a user is likely to
# type resolves.
MIN_POPULATION = 500

# Well-known places also carry their alternate spellings, so "NYC" and "LA"
# work as well as the full name.
ALIAS_POPULATION = 100_000
MAX_ALIAS_LENGTH = 30


class Command(BaseCommand):
    help = "Build data/us_places.csv, the offline gazetteer for endpoint resolution"

    def add_arguments(self, parser):
        parser.add_argument(
            "--geonames",
            type=Path,
            default=settings.BASE_DIR / "data" / "geonames_raw" / "US.txt",
        )
        parser.add_argument("--output", type=Path, default=settings.PLACES_CSV)

    def handle(self, *args, **options):
        geonames: Path = options["geonames"]
        output: Path = options["output"]

        if not geonames.exists():
            raise CommandError(
                f"GeoNames dump not found: {geonames}\n"
                "Download it with:\n"
                "  curl -L -o data/geonames_raw/US.zip "
                "https://download.geonames.org/export/dump/US.zip\n"
                "  unzip -o data/geonames_raw/US.zip -d data/geonames_raw"
            )

        wanted = self._station_cities()
        self.stdout.write(f"  {len(wanted):,} distinct truckstop cities must resolve")

        collected: dict[tuple[str, str], tuple[float, float, int]] = {}
        aliases = 0

        with geonames.open(encoding="utf-8", newline="") as handle:
            for parts in csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
                if len(parts) <= COL_POPULATION:
                    continue
                if parts[COL_FEATURE_CLASS] != POPULATED_CLASS:
                    continue
                if parts[COL_FEATURE_CODE] in HISTORICAL_CODES:
                    continue

                state = parts[COL_ADMIN1].strip().upper()
                if len(state) != 2:
                    continue

                try:
                    latitude = float(parts[COL_LAT])
                    longitude = float(parts[COL_LON])
                    population = int(parts[COL_POPULATION] or 0)
                except ValueError:
                    continue

                names = {parts[COL_NAME].strip(), parts[COL_ASCII].strip()}
                is_station_city = any((name.upper(), state) in wanted for name in names)

                if population < MIN_POPULATION and not is_station_city:
                    continue

                if population >= ALIAS_POPULATION:
                    for alias in parts[COL_ALTERNATES].split(","):
                        alias = alias.strip()
                        if alias and alias.isascii() and len(alias) <= MAX_ALIAS_LENGTH:
                            names.add(alias)
                            aliases += 1

                for name in names:
                    if not name:
                        continue
                    key = (name, state)
                    existing = collected.get(key)
                    if existing is None or population > existing[2]:
                        collected[key] = (latitude, longitude, population)

        # Station cities are resolved through the same index that geocoded the
        # stations, so anything reachable only by an alternate spelling or the
        # prefix fallback still lands here under the spelling the file uses.
        recovered = 0
        city_index = CityIndex.from_dump(geonames)
        already = {(normalize_city(name), state) for name, state in collected}

        for city, state in wanted:
            key = (normalize_city(city), state)
            if key in already:
                continue
            position = city_index.lookup(city, state)
            if position is None:
                continue
            collected[(city.title(), state)] = (position[0], position[1], 0)
            already.add(key)
            recovered += 1

        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            writer.writeheader()
            for (name, state), (latitude, longitude, population) in sorted(collected.items()):
                writer.writerow(
                    {
                        "name": name,
                        "state": state,
                        "latitude": f"{latitude:.5f}",
                        "longitude": f"{longitude:.5f}",
                        "population": population,
                    }
                )

        size_mb = output.stat().st_size / 1024 / 1024
        self.stdout.write(
            f"  {len(collected):,} entries "
            f"({aliases:,} alternate spellings, {recovered:,} recovered truckstop cities)"
        )
        self.stdout.write(self.style.SUCCESS(f"Wrote {output} ({size_mb:.1f} MB)"))

    def _station_cities(self) -> set[tuple[str, str]]:
        """Cities that must resolve because a truckstop sits in them."""
        source = settings.STATIONS_GEOCODED_CSV
        if not source.exists():
            raise CommandError(
                f"{source} not found. Run build_station_data first."
            )
        with source.open(newline="", encoding="utf-8") as handle:
            return {
                (row["city"].strip().upper(), row["state"].strip().upper())
                for row in csv.DictReader(handle)
            }
