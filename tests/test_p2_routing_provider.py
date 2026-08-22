"""P2 gate: one call out, correct units, and failures that name themselves.

Every test here is mocked. The brief's "one call is ideal" requirement is not
a comment in the README, it is asserted below.
"""

import httpx
import pytest
import respx

from apps.routing import polyline
from apps.routing.providers import (
    Coordinate,
    OSRMProvider,
    RouteNotFound,
    RoutingRequestInvalid,
    RoutingUnavailable,
    ValhallaProvider,
    fetch_route,
    get_provider,
    reset_client,
)

DALLAS = Coordinate(32.78306, -96.80667)
CHICAGO = Coordinate(41.85003, -87.65005)


@pytest.fixture(autouse=True)
def fresh_http_client():
    """Each test gets an unpooled client so respx sees every request."""
    reset_client()
    yield
    reset_client()


def osrm_body(geometry: str, metres: float, seconds: float) -> dict:
    return {
        "code": "Ok",
        "routes": [{"geometry": geometry, "distance": metres, "duration": seconds, "legs": []}],
        "waypoints": [],
    }


# --------------------------------------------------------------------------
# Polyline codec
# --------------------------------------------------------------------------


def test_decodes_the_canonical_google_test_vector():
    """The reference string from Google's polyline specification."""
    points = polyline.decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)

    assert points == pytest.approx([(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)])


def test_round_trips_at_precision_six():
    original = [(32.78306, -96.80667), (35.4676, -97.51643), (41.85003, -87.65005)]

    restored = polyline.decode(polyline.encode(original, 6), 6)

    assert restored == pytest.approx(original, abs=1e-6)


def test_empty_polyline_decodes_to_nothing():
    assert polyline.decode("") == []


def test_truncated_polyline_is_rejected_rather_than_guessed():
    with pytest.raises(ValueError, match="Truncated polyline"):
        polyline.decode("_p~iF~ps|U_ulL")


# --------------------------------------------------------------------------
# The one-call requirement
# --------------------------------------------------------------------------


@respx.mock
def test_a_successful_route_makes_exactly_one_external_call():
    geometry = polyline.encode([(32.78, -96.80), (37.0, -92.0), (41.85, -87.65)], 6)
    mocked = respx.get(url__startswith="https://router.project-osrm.org/route/v1/driving/").mock(
        return_value=httpx.Response(200, json=osrm_body(geometry, 1_555_000.0, 61_500.0))
    )

    OSRMProvider().route(DALLAS, CHICAGO)

    assert mocked.call_count == 1


@respx.mock
def test_the_request_asks_for_full_geometry_in_one_shot():
    """Corridor matching needs shape points, so a coarse overview will not do."""
    geometry = polyline.encode([(32.78, -96.80), (41.85, -87.65)], 6)
    mocked = respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(200, json=osrm_body(geometry, 1_555_000.0, 61_500.0))
    )

    OSRMProvider().route(DALLAS, CHICAGO)

    query = mocked.calls[0].request.url.params
    assert query["overview"] == "full"
    assert query["geometries"] == "polyline6"
    assert query["alternatives"] == "false"


# --------------------------------------------------------------------------
# Parsing and units
# --------------------------------------------------------------------------


@respx.mock
def test_metres_and_seconds_become_miles_and_hours():
    geometry = polyline.encode([(32.78, -96.80), (41.85, -87.65)], 6)
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(200, json=osrm_body(geometry, 1_609_344.0, 36_000.0))
    )

    result = OSRMProvider().route(DALLAS, CHICAGO)

    assert result.distance_miles == pytest.approx(1000.0)
    assert result.duration_hours == pytest.approx(10.0)
    assert result.api_calls == 1
    assert result.provider == "osrm"


@respx.mock
def test_geojson_output_flips_to_longitude_first():
    """We carry (lat, lon); GeoJSON requires (lon, lat). Getting this backwards
    would place the whole route in the wrong hemisphere."""
    geometry = polyline.encode([(32.78306, -96.80667), (41.85003, -87.65005)], 6)
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(200, json=osrm_body(geometry, 1000.0, 100.0))
    )

    geojson = OSRMProvider().route(DALLAS, CHICAGO).as_geojson()

    assert geojson["type"] == "LineString"
    assert geojson["coordinates"][0] == [-96.80667, 32.78306]


@respx.mock
def test_bbox_is_west_south_east_north():
    geometry = polyline.encode([(32.0, -96.0), (41.0, -87.0)], 6)
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(200, json=osrm_body(geometry, 1000.0, 100.0))
    )

    west, south, east, north = OSRMProvider().route(DALLAS, CHICAGO).bbox()

    assert (west, south, east, north) == pytest.approx((-96.0, 32.0, -87.0, 41.0))


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------


@respx.mock
def test_unroutable_points_raise_route_not_found():
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(200, json={"code": "NoRoute", "message": "no route"})
    )

    with pytest.raises(RouteNotFound):
        OSRMProvider().route(DALLAS, CHICAGO)


@respx.mock
def test_server_error_raises_unavailable():
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(RoutingUnavailable, match="503"):
        OSRMProvider().route(DALLAS, CHICAGO)


@respx.mock
def test_rate_limiting_is_named_explicitly():
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(429)
    )

    with pytest.raises(RoutingUnavailable, match="rate limited"):
        OSRMProvider().route(DALLAS, CHICAGO)


@respx.mock
def test_timeout_raises_unavailable_not_a_bare_httpx_error():
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        side_effect=httpx.ReadTimeout("too slow")
    )

    with pytest.raises(RoutingUnavailable, match="timed out"):
        OSRMProvider().route(DALLAS, CHICAGO)


@respx.mock
def test_degenerate_geometry_is_rejected():
    """A one-point line cannot be matched against, so fail loudly."""
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(
            200, json=osrm_body(polyline.encode([(32.0, -96.0)], 6), 0.0, 0.0)
        )
    )

    with pytest.raises(RoutingUnavailable, match="degenerate"):
        OSRMProvider().route(DALLAS, CHICAGO)


def test_out_of_range_coordinates_are_caught_before_any_request():
    with pytest.raises(ValueError, match="Latitude out of range"):
        Coordinate(91.0, -96.0)
    with pytest.raises(ValueError, match="Longitude out of range"):
        Coordinate(32.0, -181.0)


# --------------------------------------------------------------------------
# Fallback behaviour
# --------------------------------------------------------------------------


@respx.mock
def test_fallback_stays_out_of_the_way_when_the_primary_works(settings):
    settings.ROUTING_FALLBACK_PROVIDER = "valhalla"
    geometry = polyline.encode([(32.78, -96.80), (41.85, -87.65)], 6)
    osrm = respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(200, json=osrm_body(geometry, 1_609_344.0, 3_600.0))
    )
    valhalla = respx.post(url__startswith="https://valhalla1.openstreetmap.de/").mock(
        return_value=httpx.Response(200, json={})
    )

    result = fetch_route(DALLAS, CHICAGO)

    assert osrm.call_count == 1
    assert valhalla.call_count == 0
    assert result.api_calls == 1


@respx.mock
def test_fallback_recovers_when_the_primary_is_down(settings):
    settings.ROUTING_FALLBACK_PROVIDER = "valhalla"
    geometry = polyline.encode([(32.78, -96.80), (41.85, -87.65)], 6)
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(503)
    )
    respx.post(url__startswith="https://valhalla1.openstreetmap.de/").mock(
        return_value=httpx.Response(
            200,
            json={
                "trip": {
                    "legs": [{"shape": geometry}],
                    "summary": {"length": 966.0, "time": 61_500.0},
                }
            },
        )
    )

    result = fetch_route(DALLAS, CHICAGO)

    assert result.provider == "valhalla"
    assert result.distance_miles == pytest.approx(966.0)
    # Both attempts are reported; the response must not understate its egress.
    assert result.api_calls == 2


@respx.mock
def test_no_route_does_not_trigger_a_second_provider(settings):
    """A definitive answer. Asking again would only spend another call."""
    settings.ROUTING_FALLBACK_PROVIDER = "valhalla"
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(200, json={"code": "NoRoute"})
    )
    valhalla = respx.post(url__startswith="https://valhalla1.openstreetmap.de/").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(RouteNotFound):
        fetch_route(DALLAS, CHICAGO)

    assert valhalla.call_count == 0


@respx.mock
def test_failure_propagates_when_no_fallback_is_configured(settings):
    settings.ROUTING_FALLBACK_PROVIDER = ""
    respx.get(url__startswith="https://router.project-osrm.org/").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(RoutingUnavailable):
        fetch_route(DALLAS, CHICAGO)


def test_provider_registry_rejects_unknown_names():
    with pytest.raises(RoutingRequestInvalid, match="Unknown routing provider"):
        get_provider("google-maps")


def test_default_provider_needs_no_credentials(settings):
    provider = get_provider()
    assert provider.name == "osrm"
    assert "key" not in provider.base_url.lower()
