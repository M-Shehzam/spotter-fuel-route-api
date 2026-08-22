import httpx
import pytest
import respx
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command

from apps.routing import polyline


@pytest.fixture(autouse=True)
def clean_cache():
    """Route results are cached, so tests must not inherit each other's."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def fresh_indexes():
    """Station and place indexes are process-wide singletons."""
    from apps.routing.corridor import reset_station_index
    from apps.routing.providers import reset_client
    from apps.routing.resolver import reset_place_index

    reset_station_index()
    reset_place_index()
    reset_client()
    yield
    reset_station_index()
    reset_place_index()
    reset_client()


@pytest.fixture
def osrm():
    """Mock OSRM returning a plausible Dallas-to-Chicago route.

    Geometry runs through Oklahoma, Missouri and Illinois so that genuine
    loaded truckstops fall inside the corridor.
    """
    waypoints = [
        (32.78306, -96.80667), (33.62, -96.60), (34.75, -96.40), (35.47, -96.20),
        (36.15, -95.99), (37.09, -94.51), (38.35, -93.20), (38.63, -90.20),
        (39.80, -89.65), (40.69, -89.59), (41.52, -88.08), (41.85003, -87.65005),
    ]
    dense = []
    for (lat1, lon1), (lat2, lon2) in zip(waypoints, waypoints[1:]):
        for step in range(120):
            fraction = step / 120
            dense.append((lat1 + (lat2 - lat1) * fraction, lon1 + (lon2 - lon1) * fraction))
    dense.append(waypoints[-1])

    body = {
        "code": "Ok",
        "routes": [
            {
                "geometry": polyline.encode(dense, 6),
                "distance": 966.29 * 1609.344,
                "duration": 17.09 * 3600,
                "legs": [],
            }
        ],
        "waypoints": [],
    }

    with respx.mock(assert_all_called=False) as router:
        route = router.get(url__startswith="https://router.project-osrm.org/").mock(
            return_value=httpx.Response(200, json=body)
        )
        yield route


@pytest.fixture(scope="session")
def stations_loaded(django_db_setup, django_db_blocker):
    """Load the committed geocoded station file into the test database once."""
    with django_db_blocker.unblock():
        call_command("load_stations", "--truncate", verbosity=0)
    yield


@pytest.fixture
def price_csv(tmp_path):
    """A miniature price file exercising every cleaning rule."""
    path = tmp_path / "prices.csv"
    path.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        # Three observations of one station, mean 3.20
        "1,PILOT TRAVEL CENTER #1,\"I-10, EXIT 5\",Dallas,TX,100,3.10\n"
        "1,PILOT #1,\"I-10, EXIT 5\",Dallas,TX,100,3.20\n"
        "1,PILOT #1,\"I-10, EXIT 5\",Dallas,TX,100,3.30\n"
        # Single observation
        "2,LOVES #2,\"I-40, EXIT 9\",Tomah,WI,200,3.50\n"
        # Canadian, must be dropped
        "3,HUSKY CALGARY,\"HWY 1\",Calgary,AB,300,4.10\n"
        # Unparseable price, must be dropped
        "4,BROKEN STOP,\"I-5\",Portland,OR,400,not-a-price\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def geonames_dump(tmp_path):
    """A miniature GeoNames dump in the real tab-separated column layout."""
    path = tmp_path / "US.txt"

    def row(name, state, lat, lon, population, code="PPL", alternates=""):
        parts = [""] * 19
        parts[1] = name
        parts[2] = name
        parts[3] = alternates
        parts[4] = str(lat)
        parts[5] = str(lon)
        parts[6] = "P"
        parts[7] = code
        parts[10] = state
        parts[14] = str(population)
        return "\t".join(parts)

    path.write_text(
        "\n".join(
            [
                row("Dallas", "TX", 32.78306, -96.80667, 1300000),
                # A tiny namesake that must lose to the city above.
                row("Dallas", "TX", 30.0, -95.0, 40),
                row("Tomah", "WI", 43.97858, -90.50402, 9000),
                row("O'Neill", "NE", 42.45778, -98.64754, 3700),
                row("Brook Park", "OH", 41.39838, -81.80236, 18000),
                row("Saint Louis", "MO", 38.62727, -90.19789, 300000),
                # Historical place, must never be indexed.
                row("Ghosttown", "NV", 39.0, -117.0, 0, code="PPLQ"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path
