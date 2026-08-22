import pytest
from django.conf import settings
from django.core.management import call_command


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
