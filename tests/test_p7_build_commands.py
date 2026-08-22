"""P7 gate: the build-time data commands, and the paths the phase suites miss.

The two build commands regenerate everything the repo ships. A reviewer who
runs them must get the same data back, so they are covered here rather than
trusted because they ran once on my machine.

Author: Muhammad Shehzam
"""

import csv

import httpx
import pytest
import respx
from django.core.management import CommandError, call_command

from apps.routing import polyline
from apps.routing.providers import (
    Coordinate,
    RouteNotFound,
    RoutingUnavailable,
    ValhallaProvider,
    reset_client,
)
from apps.stations.geocoding import CityIndex

DALLAS = Coordinate(32.78306, -96.80667)
CHICAGO = Coordinate(41.85003, -87.65005)


@pytest.fixture(autouse=True)
def unpooled_client():
    reset_client()
    yield
    reset_client()


# --------------------------------------------------------------------------
# build_station_data
# --------------------------------------------------------------------------


def test_building_station_data_cleans_and_geocodes(tmp_path, price_csv, geonames_dump, capsys):
    output = tmp_path / "stations.csv"

    call_command(
        "build_station_data",
        source=price_csv,
        geonames=geonames_dump,
        output=output,
    )

    rows = list(csv.DictReader(output.open(encoding="utf-8")))
    by_id = {int(row["opis_id"]): row for row in rows}

    # The Canadian row and the unparseable one are gone.
    assert set(by_id) == {1, 2}

    pilot = by_id[1]
    assert pilot["retail_price"] == "3.20000"
    assert pilot["price_sample_count"] == "3"
    assert pilot["geocode_precision"] == "city"
    assert float(pilot["latitude"]) == pytest.approx(32.78306)

    printed = capsys.readouterr().out
    assert "dropped, outside the USA" in printed


def test_building_station_data_marks_what_it_could_not_place(
    tmp_path, geonames_dump
):
    prices = tmp_path / "prices.csv"
    prices.write_text(
        "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
        "9,GHOST STOP,\"I-1\",Atlantis,TX,900,3.50\n",
        encoding="utf-8",
    )
    output = tmp_path / "stations.csv"

    call_command("build_station_data", source=prices, geonames=geonames_dump, output=output)

    row = next(csv.DictReader(output.open(encoding="utf-8")))
    assert row["geocode_precision"] == "unknown"
    assert row["latitude"] == ""


def test_building_station_data_refuses_without_the_geonames_dump(tmp_path, price_csv):
    with pytest.raises(CommandError, match="GeoNames dump not found"):
        call_command(
            "build_station_data",
            source=price_csv,
            geonames=tmp_path / "absent.txt",
            output=tmp_path / "out.csv",
        )


def test_building_station_data_refuses_without_the_price_file(tmp_path, geonames_dump):
    with pytest.raises(CommandError, match="Price file not found"):
        call_command(
            "build_station_data",
            source=tmp_path / "absent.csv",
            geonames=geonames_dump,
            output=tmp_path / "out.csv",
        )


# --------------------------------------------------------------------------
# build_places_index
# --------------------------------------------------------------------------


def test_the_gazetteer_covers_every_station_city(tmp_path, price_csv, geonames_dump):
    stations = tmp_path / "stations.csv"
    call_command("build_station_data", source=price_csv, geonames=geonames_dump, output=stations)

    places = tmp_path / "places.csv"
    call_command(
        "build_places_index", geonames=geonames_dump, stations=stations, output=places
    )

    listed = {(row["name"].upper(), row["state"]) for row in csv.DictReader(places.open())}
    for row in csv.DictReader(stations.open()):
        if row["latitude"]:
            assert (row["city"].upper(), row["state"]) in listed


def test_the_gazetteer_keeps_the_larger_of_two_namesakes(
    tmp_path, price_csv, geonames_dump
):
    """The fixture holds two places called Dallas in Texas."""
    stations = tmp_path / "stations.csv"
    call_command("build_station_data", source=price_csv, geonames=geonames_dump, output=stations)
    places = tmp_path / "places.csv"
    call_command(
        "build_places_index", geonames=geonames_dump, stations=stations, output=places
    )

    dallas = [
        row
        for row in csv.DictReader(places.open())
        if row["name"] == "Dallas" and row["state"] == "TX"
    ]

    assert len(dallas) == 1
    assert float(dallas[0]["latitude"]) == pytest.approx(32.78306)


def test_the_gazetteer_refuses_without_station_data(tmp_path, geonames_dump):
    with pytest.raises(CommandError, match="Run build_station_data first"):
        call_command(
            "build_places_index",
            geonames=geonames_dump,
            stations=tmp_path / "absent.csv",
            output=tmp_path / "places.csv",
        )


# --------------------------------------------------------------------------
# load_stations
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_loading_refuses_a_missing_file(tmp_path):
    with pytest.raises(CommandError, match="not found"):
        call_command("load_stations", input=tmp_path / "absent.csv")


@pytest.mark.django_db
def test_loading_refuses_an_empty_file(tmp_path):
    empty = tmp_path / "empty.csv"
    empty.write_text("opis_id,name,address,city,state,rack_id,retail_price,"
                     "price_sample_count,price_min,price_max,latitude,longitude,"
                     "geocode_precision\n", encoding="utf-8")

    with pytest.raises(CommandError, match="is empty"):
        call_command("load_stations", input=empty)


# --------------------------------------------------------------------------
# The standby routing provider
# --------------------------------------------------------------------------


@respx.mock
def test_valhalla_parses_a_route():
    geometry = polyline.encode([(32.78, -96.80), (37.0, -92.0), (41.85, -87.65)], 6)
    respx.post(url__startswith="https://valhalla1.openstreetmap.de/").mock(
        return_value=httpx.Response(
            200,
            json={
                "trip": {
                    "legs": [{"shape": geometry}],
                    "summary": {"length": 966.3, "time": 61_500.0},
                }
            },
        )
    )

    result = ValhallaProvider().route(DALLAS, CHICAGO)

    assert result.provider == "valhalla"
    assert result.distance_miles == pytest.approx(966.3)
    assert result.duration_hours == pytest.approx(61_500.0 / 3600.0)
    assert result.point_count == 3


@respx.mock
def test_valhalla_asks_for_truck_costing_in_miles():
    """A car route would take roads a loaded truck cannot."""
    geometry = polyline.encode([(32.78, -96.80), (41.85, -87.65)], 6)
    mocked = respx.post(url__startswith="https://valhalla1.openstreetmap.de/").mock(
        return_value=httpx.Response(
            200,
            json={"trip": {"legs": [{"shape": geometry}], "summary": {"length": 1.0, "time": 1.0}}},
        )
    )

    ValhallaProvider().route(DALLAS, CHICAGO)

    import json as jsonlib

    body = jsonlib.loads(mocked.calls[0].request.content)
    assert body["costing"] == "truck"
    assert body["units"] == "miles"


@respx.mock
def test_valhalla_joins_multiple_legs():
    first = polyline.encode([(32.78, -96.80), (35.0, -94.0)], 6)
    second = polyline.encode([(35.0, -94.0), (41.85, -87.65)], 6)
    respx.post(url__startswith="https://valhalla1.openstreetmap.de/").mock(
        return_value=httpx.Response(
            200,
            json={
                "trip": {
                    "legs": [{"shape": first}, {"shape": second}],
                    "summary": {"length": 966.0, "time": 61_500.0},
                }
            },
        )
    )

    assert ValhallaProvider().route(DALLAS, CHICAGO).point_count == 4


@respx.mock
def test_valhalla_reports_an_unroutable_pair():
    respx.post(url__startswith="https://valhalla1.openstreetmap.de/").mock(
        return_value=httpx.Response(
            400, json={"error_code": 442, "error": "No path could be found"}
        )
    )

    with pytest.raises(RouteNotFound):
        ValhallaProvider().route(DALLAS, CHICAGO)


@respx.mock
def test_valhalla_reports_being_down():
    respx.post(url__startswith="https://valhalla1.openstreetmap.de/").mock(
        return_value=httpx.Response(502)
    )

    with pytest.raises(RoutingUnavailable, match="502"):
        ValhallaProvider().route(DALLAS, CHICAGO)


@respx.mock
def test_valhalla_rejects_a_reply_with_no_legs():
    respx.post(url__startswith="https://valhalla1.openstreetmap.de/").mock(
        return_value=httpx.Response(200, json={"trip": {"legs": [], "summary": {}}})
    )

    with pytest.raises(RouteNotFound):
        ValhallaProvider().route(DALLAS, CHICAGO)


# --------------------------------------------------------------------------
# Geocoding fallbacks
# --------------------------------------------------------------------------


def test_a_name_carrying_a_suffix_still_resolves(tmp_path):
    """"Bordentown Township" should find Bordentown."""
    dump = tmp_path / "US.txt"
    parts = [""] * 19
    parts[1] = parts[2] = "Bordentown"
    parts[4], parts[5] = "40.14", "-74.71"
    parts[6], parts[7], parts[10], parts[14] = "P", "PPL", "NJ", "3900"
    dump.write_text("\t".join(parts) + "\n", encoding="utf-8")

    index = CityIndex.from_dump(dump)

    assert index.lookup("Bordentown Township", "NJ") is not None


def test_a_prefix_too_short_to_be_meaningful_is_refused(tmp_path):
    dump = tmp_path / "US.txt"
    parts = [""] * 19
    parts[1] = parts[2] = "Ely"
    parts[4], parts[5] = "39.25", "-114.88"
    parts[6], parts[7], parts[10], parts[14] = "P", "PPL", "NV", "4000"
    dump.write_text("\t".join(parts) + "\n", encoding="utf-8")

    index = CityIndex.from_dump(dump)

    # "Ely" is three letters, too ambiguous to match by prefix.
    assert index.lookup("Ely Junction Heights", "NV") is None


def test_rows_too_short_to_parse_are_skipped(tmp_path):
    dump = tmp_path / "US.txt"
    dump.write_text("1\tBroken\tBroken\n", encoding="utf-8")

    assert len(CityIndex.from_dump(dump)) == 0


def test_unparseable_coordinates_are_skipped(tmp_path):
    dump = tmp_path / "US.txt"
    parts = [""] * 19
    parts[1] = parts[2] = "Nowhere"
    parts[4], parts[5] = "not-a-number", "-100.0"
    parts[6], parts[7], parts[10], parts[14] = "P", "PPL", "TX", "500"
    dump.write_text("\t".join(parts) + "\n", encoding="utf-8")

    assert len(CityIndex.from_dump(dump)) == 0
