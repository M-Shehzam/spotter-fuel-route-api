"""Opt-in checks against the real routing API.

Excluded from the default run so the suite stays fast and offline. Run them
before recording a demo, to confirm the public server is actually up:

    pytest -m live
"""

import pytest

from apps.routing.providers import Coordinate, OSRMProvider

pytestmark = pytest.mark.live

DALLAS = Coordinate(32.78306, -96.80667)
CHICAGO = Coordinate(41.85003, -87.65005)
LOS_ANGELES = Coordinate(34.05223, -118.24368)
NEW_YORK = Coordinate(40.71427, -74.00597)


@pytest.mark.parametrize(
    ("label", "start", "finish", "low", "high"),
    [
        ("Dallas to Chicago", DALLAS, CHICAGO, 925, 1050),
        ("Los Angeles to New York", LOS_ANGELES, NEW_YORK, 2700, 2950),
    ],
)
def test_real_routes_have_believable_distances(label, start, finish, low, high):
    result = OSRMProvider().route(start, finish)

    assert low <= result.distance_miles <= high, f"{label}: {result.distance_miles:.0f} mi"
    assert result.duration_hours > 0
    assert result.api_calls == 1


def test_real_geometry_starts_and_ends_where_asked():
    result = OSRMProvider().route(DALLAS, CHICAGO)

    first_lat, first_lon = result.coordinates[0]
    last_lat, last_lon = result.coordinates[-1]

    assert first_lat == pytest.approx(DALLAS.latitude, abs=0.5)
    assert first_lon == pytest.approx(DALLAS.longitude, abs=0.5)
    assert last_lat == pytest.approx(CHICAGO.latitude, abs=0.5)
    assert last_lon == pytest.approx(CHICAGO.longitude, abs=0.5)


def test_real_geometry_is_dense_enough_for_corridor_matching():
    """Corridor search needs shape points, not a coarse two-point sketch."""
    result = OSRMProvider().route(DALLAS, CHICAGO)

    assert result.point_count > 1000
    assert all(18 < lat < 72 and -180 < lon < -64 for lat, lon in result.coordinates[::200])
