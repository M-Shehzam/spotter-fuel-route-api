"""P3 gate: measuring a route, thinning it, and finding what sits beside it."""

import math
import time

import numpy as np
import pytest

from apps.routing.corridor import (
    CELL_DEGREES,
    ROUTE_SAMPLE_MILES,
    Candidate,
    StationIndex,
    find_candidates,
    prepare_route,
)
from apps.routing.geo import EARTH_RADIUS_MILES, cumulative_miles, haversine_miles, planar_miles
from apps.routing.providers import RouteResult

# One degree of latitude is a shade over 69 miles anywhere on the globe.
MILES_PER_DEGREE_LAT = math.pi * EARTH_RADIUS_MILES / 180.0


def straight_route(
    start_lat: float = 40.0,
    start_lon: float = -100.0,
    degrees: float = 8.0,
    points: int = 4000,
) -> RouteResult:
    """A due-east route at constant latitude, so distances are predictable.

    Point spacing is kept near a tenth of a mile to match what real routing
    geometry looks like: detour is measured against the original vertices, so a
    coarse fixture would put a floor under the smallest measurable distance.
    """
    longitudes = np.linspace(start_lon, start_lon + degrees, points)
    coordinates = [(start_lat, float(lon)) for lon in longitudes]
    span = float(
        haversine_miles(start_lat, start_lon, start_lat, start_lon + degrees)
    )
    return RouteResult(
        coordinates=coordinates,
        distance_miles=span,
        duration_hours=span / 55.0,
        provider="test",
    )


def index_of(stations: list[tuple[int, float, float, float]]) -> StationIndex:
    """Build an index from (opis_id, latitude, longitude, price) tuples."""
    return StationIndex(
        opis_ids=np.array([s[0] for s in stations], dtype=np.int64),
        latitudes=np.array([s[1] for s in stations], dtype=np.float64),
        longitudes=np.array([s[2] for s in stations], dtype=np.float64),
        prices=np.array([s[3] for s in stations], dtype=np.float64),
    )


# --------------------------------------------------------------------------
# Distance primitives
# --------------------------------------------------------------------------


def test_haversine_matches_a_known_separation():
    """Dallas to Chicago is about 800 miles as the crow flies."""
    miles = float(haversine_miles(32.78306, -96.80667, 41.85003, -87.65005))
    assert 790 < miles < 810


def test_haversine_is_zero_for_a_point_against_itself():
    assert float(haversine_miles(41.0, -87.0, 41.0, -87.0)) == pytest.approx(0.0, abs=1e-9)


def test_one_degree_of_latitude_is_about_sixty_nine_miles():
    miles = float(haversine_miles(40.0, -100.0, 41.0, -100.0))
    assert miles == pytest.approx(MILES_PER_DEGREE_LAT, rel=1e-6)


def test_planar_approximation_tracks_haversine_across_the_corridor():
    """The approximation is only used inside the corridor, so that is where it
    has to hold. A tenth of a mile over ten is far tighter than we need."""
    latitude = 41.0
    cos_reference = math.cos(math.radians(latitude))

    for offset_miles in (1.0, 5.0, 10.0, 25.0):
        delta = offset_miles / MILES_PER_DEGREE_LAT
        exact = float(haversine_miles(latitude, -100.0, latitude + delta, -100.0))
        approximate = float(
            planar_miles(
                np.array([latitude]),
                np.array([-100.0]),
                np.array([latitude + delta]),
                np.array([-100.0]),
                cos_reference,
            )[0]
        )
        assert approximate == pytest.approx(exact, rel=0.002)


def test_cumulative_distance_starts_at_zero_and_increases():
    latitudes = np.array([40.0, 40.0, 40.0])
    longitudes = np.array([-100.0, -99.0, -98.0])

    measured = cumulative_miles(latitudes, longitudes)

    assert measured[0] == 0.0
    assert measured[1] < measured[2]
    assert measured[2] == pytest.approx(2 * measured[1], rel=1e-9)


# --------------------------------------------------------------------------
# Route preparation
# --------------------------------------------------------------------------


def test_thinning_keeps_both_endpoints():
    """The first and last points are the origin and destination."""
    route = straight_route(points=5000)

    geometry = prepare_route(route)

    assert geometry.latitudes[0] == pytest.approx(route.coordinates[0][0])
    assert geometry.longitudes[0] == pytest.approx(route.coordinates[0][1])
    assert geometry.longitudes[-1] == pytest.approx(route.coordinates[-1][1])
    assert geometry.mile_markers[0] == 0.0


def test_thinning_reduces_the_point_count_substantially():
    route = straight_route(points=5000)

    geometry = prepare_route(route)

    assert geometry.original_point_count == 5000
    assert geometry.sample_count < 500


def test_retained_points_sit_about_the_sample_spacing_apart():
    geometry = prepare_route(straight_route(points=5000))

    gaps = np.diff(geometry.mile_markers[:-1])

    assert gaps.max() <= ROUTE_SAMPLE_MILES * 1.5


def test_mile_markers_are_rescaled_to_the_providers_distance():
    """Great-circle hops undershoot road length. Letting the two disagree
    would make the fuel total inconsistent with the reported distance."""
    route = straight_route()
    route.distance_miles *= 1.15  # as though the road wandered

    geometry = prepare_route(route)

    assert geometry.mile_markers[-1] == pytest.approx(route.distance_miles, rel=1e-6)
    assert geometry.total_miles == pytest.approx(route.distance_miles)


def test_mile_markers_never_decrease():
    geometry = prepare_route(straight_route(points=2000))
    assert np.all(np.diff(geometry.mile_markers) >= 0)


def test_a_two_point_route_survives_preparation():
    route = RouteResult(
        coordinates=[(40.0, -100.0), (40.0, -99.0)],
        distance_miles=53.0,
        duration_hours=1.0,
        provider="test",
    )

    geometry = prepare_route(route)

    assert geometry.sample_count == 2
    assert geometry.mile_markers[-1] == pytest.approx(53.0)


# --------------------------------------------------------------------------
# Corridor matching
# --------------------------------------------------------------------------


def test_a_station_on_the_route_is_found_at_zero_detour():
    geometry = prepare_route(straight_route())
    index = index_of([(1, 40.0, -96.0, 3.50)])

    found = find_candidates(geometry, max_detour_miles=10.0, index=index)

    assert len(found) == 1
    assert found[0].opis_id == 1
    # Measured against the original vertices, so the floor is half their
    # spacing rather than the two-mile sampling interval.
    assert found[0].detour_miles == pytest.approx(0.0, abs=0.1)


def test_a_station_beyond_the_corridor_is_excluded():
    geometry = prepare_route(straight_route())
    far = 40.0 + (30.0 / MILES_PER_DEGREE_LAT)  # 30 miles north of the road
    index = index_of([(1, far, -96.0, 3.50)])

    assert find_candidates(geometry, max_detour_miles=10.0, index=index) == []


def test_the_corridor_boundary_is_honoured_in_both_directions():
    geometry = prepare_route(straight_route())
    inside = 40.0 + (8.0 / MILES_PER_DEGREE_LAT)
    outside = 40.0 + (12.0 / MILES_PER_DEGREE_LAT)
    index = index_of([(1, inside, -96.0, 3.50), (2, outside, -96.0, 3.10)])

    found = find_candidates(geometry, max_detour_miles=10.0, index=index)

    # The cheaper station is outside the corridor and must not be smuggled in.
    assert [candidate.opis_id for candidate in found] == [1]


def test_detour_distance_is_measured_not_assumed():
    geometry = prepare_route(straight_route())
    offset = 6.0 / MILES_PER_DEGREE_LAT
    index = index_of([(1, 40.0 + offset, -96.0, 3.50)])

    found = find_candidates(geometry, max_detour_miles=10.0, index=index)

    assert found[0].detour_miles == pytest.approx(6.0, rel=0.05)


def test_candidates_come_back_ordered_by_mile_marker():
    geometry = prepare_route(straight_route())
    index = index_of(
        [
            (1, 40.0, -94.0, 3.50),
            (2, 40.0, -99.0, 3.10),
            (3, 40.0, -96.0, 3.90),
        ]
    )

    found = find_candidates(geometry, max_detour_miles=10.0, index=index)

    # The route runs west to east from -100, so -99 comes first.
    assert [candidate.opis_id for candidate in found] == [2, 3, 1]
    markers = [candidate.distance_along_route_miles for candidate in found]
    assert markers == sorted(markers)


def test_mile_markers_place_stations_where_they_actually_are():
    geometry = prepare_route(straight_route())
    index = index_of([(1, 40.0, -99.0, 3.50)])

    found = find_candidates(geometry, max_detour_miles=10.0, index=index)

    expected = float(haversine_miles(40.0, -100.0, 40.0, -99.0))
    assert found[0].distance_along_route_miles == pytest.approx(expected, rel=0.01)


def test_a_station_is_reported_once_even_where_the_route_doubles_back():
    """An out-and-back route passes the same station twice; it is still one
    station, reported at its closest approach."""
    out = [(40.0, -100.0 + i * 0.02) for i in range(200)]
    back = [(40.05, -96.0 + i * 0.02) for i in range(200)]
    coordinates = out + list(reversed(back))
    route = RouteResult(
        coordinates=coordinates, distance_miles=440.0, duration_hours=8.0, provider="test"
    )
    index = index_of([(1, 40.0, -98.0, 3.50)])

    found = find_candidates(prepare_route(route), max_detour_miles=10.0, index=index)

    assert len(found) == 1


def test_an_empty_index_yields_no_candidates():
    geometry = prepare_route(straight_route())
    assert find_candidates(geometry, index=index_of([])) == []


def test_cells_are_sized_wider_than_the_default_corridor():
    """A single ring of neighbours only suffices while this holds."""
    from django.conf import settings

    shortest_cell_miles = CELL_DEGREES * 69.0 * math.cos(math.radians(49.0))
    assert shortest_cell_miles > settings.MAX_DETOUR_MILES


# --------------------------------------------------------------------------
# Integration with P1 and P2
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_index_loads_the_real_stations(stations_loaded):
    from apps.routing.corridor import reset_station_index, get_station_index

    reset_station_index()
    index = get_station_index()

    assert len(index) == 6623
    assert index.prices.min() > 2.0
    assert index.prices.max() < 8.0
    reset_station_index()


@pytest.mark.django_db
def test_a_real_corridor_finds_real_stations(stations_loaded):
    """A synthetic route across Missouri and Illinois, matched against the
    genuine loaded dataset rather than fixtures."""
    from apps.routing.corridor import reset_station_index, get_station_index

    reset_station_index()
    route = straight_route(start_lat=39.0, start_lon=-94.5, degrees=6.0, points=1200)

    found = find_candidates(prepare_route(route), index=get_station_index())

    assert len(found) > 5
    assert all(isinstance(candidate, Candidate) for candidate in found)
    assert all(candidate.detour_miles <= 10.0 for candidate in found)
    assert all(2.0 < candidate.price < 8.0 for candidate in found)
    reset_station_index()


@pytest.mark.django_db
def test_matching_a_transcontinental_route_stays_fast(stations_loaded):
    """The whole point of thinning and bucketing. A naive scan of 6,623
    stations against every shape point would be orders of magnitude slower."""
    from apps.routing.corridor import reset_station_index, get_station_index

    reset_station_index()
    index = get_station_index()
    geometry = prepare_route(straight_route(start_lat=39.5, start_lon=-118.0, degrees=44.0, points=30000))

    began = time.perf_counter()
    found = find_candidates(geometry, index=index)
    elapsed_ms = (time.perf_counter() - began) * 1000

    assert found
    assert elapsed_ms < 250, f"corridor matching took {elapsed_ms:.0f} ms"
    reset_station_index()
