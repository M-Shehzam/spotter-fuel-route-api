"""P5 gate: the HTTP surface, endpoint resolution, and the map page."""

import httpx
import pytest
import respx

from apps.routing.resolver import LocationNotFound, PlaceIndex, resolve

pytestmark = pytest.mark.django_db

ROUTE_URL = "/api/v1/route/"


# --------------------------------------------------------------------------
# Resolving what a user types, without calling anything
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("Dallas, TX", "Dallas, TX"),
        ("dallas, tx", "Dallas, TX"),
        ("Dallas, Texas", "Dallas, TX"),
        ("Dallas", "Dallas, TX"),
        ("Tomah WI", "Tomah, WI"),
        ("Chicago, IL, USA", "Chicago, IL"),
    ],
)
def test_place_names_resolve_offline(typed, expected):
    assert resolve(typed).label == expected


def test_coordinates_are_accepted_directly():
    location = resolve("32.7767,-96.7970")

    assert location.source == "coordinates"
    assert location.latitude == pytest.approx(32.7767)
    assert location.longitude == pytest.approx(-96.7970)


def test_well_known_abbreviations_resolve():
    assert resolve("NYC").label.endswith(", NY")
    assert resolve("LA").label.endswith(", CA")


def test_every_truckstop_city_can_be_resolved(stations_loaded):
    """The gazetteer is built to cover them, so a miss means it drifted."""
    from apps.stations.models import Station

    sample = Station.objects.geocoded().values_list("city", "state")[:400]
    for city, state in sample:
        assert resolve(f"{city}, {state}") is not None


def test_a_place_outside_the_usa_is_refused():
    """London's coordinates. The brief scopes this to the United States."""
    with pytest.raises(LocationNotFound, match="outside the USA"):
        resolve("51.5074,-0.1278")


def test_nonsense_is_refused_with_a_usable_hint():
    with pytest.raises(LocationNotFound) as caught:
        resolve("Nowhereville, ZZ")

    assert "Dallas, TX" in str(caught.value)


def test_empty_input_is_refused():
    with pytest.raises(LocationNotFound):
        resolve("")


# --------------------------------------------------------------------------
# The one-call requirement, over HTTP
# --------------------------------------------------------------------------


def test_planning_a_journey_makes_exactly_one_external_call(client, stations_loaded, osrm):
    """The brief's headline constraint, asserted end to end through the API."""
    response = client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert osrm.call_count == 1
    assert response.json()["meta"]["external_api_calls"] == 1


def test_a_repeated_journey_makes_no_external_call(client, stations_loaded, osrm):
    payload = {"start": "Dallas, TX", "finish": "Chicago, IL"}

    first = client.post(ROUTE_URL, payload, content_type="application/json").json()
    second = client.post(ROUTE_URL, payload, content_type="application/json").json()

    assert osrm.call_count == 1
    assert second["meta"]["cached"] is True
    assert second["meta"]["external_api_calls"] == 0
    assert second["fuel"]["total_cost_usd"] == first["fuel"]["total_cost_usd"]


def test_the_same_journey_phrased_differently_shares_one_call(client, stations_loaded, osrm):
    """Caching on resolved coordinates, not on the words typed."""
    client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL"},
        content_type="application/json",
    )
    response = client.get(ROUTE_URL, {"start": "dallas", "finish": "Chicago, Illinois"})

    assert osrm.call_count == 1
    assert response.json()["meta"]["cached"] is True


def test_a_different_corridor_width_is_planned_afresh(client, stations_loaded, osrm):
    """It is a different question, so it must not reuse the answer."""
    client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL"},
        content_type="application/json",
    )
    client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL", "max_detour_miles": 3},
        content_type="application/json",
    )

    assert osrm.call_count == 2


# --------------------------------------------------------------------------
# The payload
# --------------------------------------------------------------------------


def test_the_response_answers_every_part_of_the_brief(client, stations_loaded, osrm):
    body = client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL"},
        content_type="application/json",
    ).json()

    # "Return a map of the route"
    assert body["route"]["geometry"]["type"] == "LineString"
    assert len(body["route"]["geometry"]["coordinates"]) > 100
    assert body["meta"]["map_url"].startswith("/api/v1/route/map/")

    # "the optimal location to fuel up along the route"
    assert body["fuel"]["stops_count"] >= 1
    assert body["fuel_stops"][0]["name"]

    # "the total money spent on fuel assuming 10 miles per gallon"
    assert body["fuel"]["total_gallons"] == pytest.approx(
        body["route"]["total_distance_miles"] / 10, rel=1e-3
    )
    assert body["fuel"]["total_cost_usd"] > 0

    # "maximum range of 500 miles"
    assert body["vehicle"]["max_range_miles"] == 500.0
    assert body["vehicle"]["tank_gallons"] == 50.0


def test_geometry_is_simplified_rather_than_dumped_whole(client, stations_loaded, osrm):
    """Lean responses: the shape points are thinned before serialising."""
    route = client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL"},
        content_type="application/json",
    ).json()["route"]

    assert route["simplified_points"] < route["shape_points"] / 2
    assert len(route["geometry"]["coordinates"]) == route["simplified_points"]


def test_geometry_is_longitude_first_as_geojson_requires(client, stations_loaded, osrm):
    first = client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL"},
        content_type="application/json",
    ).json()["route"]["geometry"]["coordinates"][0]

    longitude, latitude = first
    assert -100 < longitude < -90
    assert 30 < latitude < 45


def test_stops_carry_the_detail_a_driver_needs(client, stations_loaded, osrm):
    stop = client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL"},
        content_type="application/json",
    ).json()["fuel_stops"][0]

    for field in (
        "name", "address", "city", "state", "latitude", "longitude",
        "price_per_gallon", "gallons", "cost_usd",
        "distance_from_start_miles", "detour_miles",
    ):
        assert field in stop, f"missing {field}"

    assert stop["cost_usd"] == pytest.approx(
        stop["gallons"] * stop["price_per_gallon"], rel=1e-2
    )


def test_savings_are_reported_against_the_corridor_average(client, stations_loaded, osrm):
    fuel = client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL"},
        content_type="application/json",
    ).json()["fuel"]

    assert fuel["naive_cost_usd"] >= fuel["total_cost_usd"]
    assert fuel["savings_usd"] == pytest.approx(
        fuel["naive_cost_usd"] - fuel["total_cost_usd"], abs=0.02
    )


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------


def test_an_unresolvable_place_is_a_bad_request(client, stations_loaded):
    response = client.post(
        ROUTE_URL,
        {"start": "Nowhereville, ZZ", "finish": "Chicago, IL"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"] == "location_not_found"


def test_a_missing_field_is_a_bad_request(client, stations_loaded):
    response = client.post(ROUTE_URL, {"start": "Dallas, TX"}, content_type="application/json")

    assert response.status_code == 400
    assert "finish" in response.json()


def test_start_and_finish_must_differ(client, stations_loaded):
    response = client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Dallas, TX"},
        content_type="application/json",
    )

    assert response.status_code == 400


def test_an_unroutable_pair_is_a_not_found(client, stations_loaded):
    with respx.mock:
        respx.get(url__startswith="https://router.project-osrm.org/").mock(
            return_value=httpx.Response(200, json={"code": "NoRoute"})
        )
        response = client.post(
            ROUTE_URL,
            {"start": "Dallas, TX", "finish": "Chicago, IL"},
            content_type="application/json",
        )

    assert response.status_code == 404
    assert response.json()["error"] == "route_not_found"


def test_a_dead_routing_provider_is_a_service_unavailable(client, stations_loaded, settings):
    settings.ROUTING_FALLBACK_PROVIDER = ""
    with respx.mock:
        respx.get(url__startswith="https://router.project-osrm.org/").mock(
            return_value=httpx.Response(503)
        )
        response = client.post(
            ROUTE_URL,
            {"start": "Dallas, TX", "finish": "Chicago, IL"},
            content_type="application/json",
        )

    assert response.status_code == 503
    assert response.json()["error"] == "routing_unavailable"


def test_an_absurd_corridor_width_is_rejected(client, stations_loaded):
    response = client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL", "max_detour_miles": 500},
        content_type="application/json",
    )

    assert response.status_code == 400


# --------------------------------------------------------------------------
# The map page
# --------------------------------------------------------------------------


def test_the_map_renders_the_planned_journey(client, stations_loaded, osrm):
    body = client.post(
        ROUTE_URL,
        {"start": "Dallas, TX", "finish": "Chicago, IL"},
        content_type="application/json",
    ).json()

    page = client.get(body["meta"]["map_url"])
    html = page.content.decode()

    assert page.status_code == 200
    assert "Dallas, TX" in html and "Chicago, IL" in html
    assert "leaflet" in html.lower()
    assert html.count('data-seq=') == body["fuel"]["stops_count"]
    assert 'id="geometry-data"' in html


def test_an_unknown_map_token_explains_itself(client, stations_loaded):
    page = client.get("/api/v1/route/map/deadbeefdeadbeefdeadbeefdeadbeef/")

    assert page.status_code == 404
    assert "expired" in page.content.decode().lower()


# --------------------------------------------------------------------------
# Browsing the price data
# --------------------------------------------------------------------------


def test_stations_can_be_filtered_by_state(client, stations_loaded):
    body = client.get("/api/v1/stations/", {"state": "TX", "limit": 5}).json()

    assert body["count"] > 100
    assert all(row["state"] == "TX" for row in body["results"])


def test_stations_are_cheapest_first_by_default(client, stations_loaded):
    rows = client.get("/api/v1/stations/", {"limit": 20}).json()["results"]

    prices = [float(row["retail_price"]) for row in rows]
    assert prices == sorted(prices)


def test_stations_expose_the_price_spread_behind_each_average(client, stations_loaded):
    rows = client.get("/api/v1/stations/", {"search": "PILOT", "limit": 50}).json()["results"]

    assert rows
    for row in rows:
        assert float(row["price_min"]) <= float(row["retail_price"]) <= float(row["price_max"])


def test_the_openapi_schema_documents_the_route_endpoint(client):
    schema = client.get("/api/schema/").content.decode()

    assert "/api/v1/route/" in schema
    assert "max_detour_miles" in schema
