"""Build the in-process indexes before the first request arrives.

Both indexes are lazy singletons, so without this the first caller after a
deploy pays to build them. Running this at container start moves that cost off
the request path.
"""

import time

from django.core.management.base import BaseCommand

from apps.routing.corridor import get_station_index
from apps.routing.resolver import get_place_index


class Command(BaseCommand):
    help = "Load the station and place indexes into memory"

    def handle(self, *args, **options):
        began = time.perf_counter()
        stations = get_station_index()
        station_ms = (time.perf_counter() - began) * 1000

        began = time.perf_counter()
        places = get_place_index()
        place_ms = (time.perf_counter() - began) * 1000

        self.stdout.write(
            f"  stations  {len(stations):>7,} in {len(stations.cells):,} cells  {station_ms:7.1f} ms"
        )
        self.stdout.write(f"  places    {len(places):>7,}                     {place_ms:7.1f} ms")

        if len(stations) == 0:
            self.stdout.write(
                self.style.WARNING("  no stations loaded; run load_stations first")
            )
            return

        self.stdout.write(self.style.SUCCESS("Indexes warm"))
