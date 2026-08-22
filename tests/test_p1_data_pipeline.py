"""P1 gate: the price file becomes clean, geocoded, queryable stations."""

from decimal import Decimal

import pytest
from django.conf import settings

from apps.stations.cleaning import CANADIAN_REGIONS, clean
from apps.stations.geocoding import CityIndex, normalize_city
from apps.stations.models import GeocodePrecision, Station


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------


def test_canadian_rows_are_dropped(price_csv):
    records, report = clean(price_csv)

    assert report.dropped_non_us == 1
    assert report.dropped_states == {"AB"}
    assert all(record.state not in CANADIAN_REGIONS for record in records)


def test_unparseable_rows_are_dropped_not_guessed(price_csv):
    _, report = clean(price_csv)
    assert report.dropped_unparseable == 1


def test_repeat_observations_collapse_to_their_mean(price_csv):
    records, report = clean(price_csv)
    pilot = next(record for record in records if record.opis_id == 1)

    # 3.10, 3.20, 3.30 -> mean 3.20, not the 3.10 minimum.
    assert pilot.retail_price == Decimal("3.20000")
    assert pilot.price_sample_count == 3
    assert pilot.price_min == Decimal("3.10000")
    assert pilot.price_max == Decimal("3.30000")
    assert report.stations_with_multiple_prices == 1


def test_the_fullest_trade_name_wins(price_csv):
    records, _ = clean(price_csv)
    pilot = next(record for record in records if record.opis_id == 1)
    assert pilot.name == "PILOT TRAVEL CENTER #1"


def test_single_observation_keeps_a_zero_spread(price_csv):
    records, _ = clean(price_csv)
    loves = next(record for record in records if record.opis_id == 2)
    assert loves.price_sample_count == 1
    assert loves.price_min == loves.price_max == loves.retail_price


def test_the_supplied_file_reduces_as_expected():
    """Pin the real numbers so a future change to the rules is visible."""
    records, report = clean(settings.STATIONS_CSV)

    assert report.raw_rows == 8151
    assert report.dropped_non_us == 620
    assert report.dropped_unparseable == 0
    assert len(records) == 6626
    assert report.widest_price_spread == Decimal("0.90000")


# --------------------------------------------------------------------------
# Geocoding
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("St. Louis", "SAINT LOUIS"),
        ("Ft. Worth", "FORT WORTH"),
        ("W Memphis", "WEST MEMPHIS"),
        ("N Little Rock", "NORTH LITTLE ROCK"),
        ("O'Neill", "ONEILL"),
        ("  Mt.  Vernon  ", "MOUNT VERNON"),
    ],
)
def test_city_names_fold_to_a_comparable_key(raw, expected):
    assert normalize_city(raw) == expected


def test_index_prefers_the_most_populous_namesake(geonames_dump):
    index = CityIndex.from_dump(geonames_dump)
    latitude, longitude = index.lookup("Dallas", "TX")

    assert latitude == pytest.approx(32.78306)
    assert longitude == pytest.approx(-96.80667)


def test_index_skips_historical_places(geonames_dump):
    index = CityIndex.from_dump(geonames_dump)
    assert index.lookup("Ghosttown", "NV") is None


def test_index_resolves_apostrophes_and_spacing_differences(geonames_dump):
    index = CityIndex.from_dump(geonames_dump)

    # Written without its apostrophe in the price file.
    assert index.lookup("Oneill", "NE") is not None
    # Written unspaced in the price file, spaced in GeoNames.
    assert index.lookup("Brookpark", "OH") is not None
    # Abbreviated in the price file, spelled out in GeoNames.
    assert index.lookup("St. Louis", "MO") is not None


def test_index_rejects_a_city_in_the_wrong_state(geonames_dump):
    index = CityIndex.from_dump(geonames_dump)
    assert index.lookup("Tomah", "TX") is None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_station_loads(stations_loaded):
    assert Station.objects.count() == 6626


@pytest.mark.django_db
def test_geocoding_coverage_stays_above_ninety_nine_percent(stations_loaded):
    total = Station.objects.count()
    geocoded = Station.objects.geocoded().count()

    assert geocoded == 6623
    assert geocoded / total > 0.99


@pytest.mark.django_db
def test_no_canadian_stations_survive_the_load(stations_loaded):
    assert not Station.objects.in_states(CANADIAN_REGIONS).exists()


@pytest.mark.django_db
def test_loaded_coordinates_are_inside_the_united_states(stations_loaded):
    """A sign error or a swapped pair would put stations off the continent."""
    outside = Station.objects.geocoded().exclude(
        latitude__gte=18.0,
        latitude__lte=72.0,
        longitude__gte=-180.0,
        longitude__lte=-64.0,
    )
    assert not outside.exists(), list(outside[:5])


@pytest.mark.django_db
def test_prices_are_plausible_diesel_retail(stations_loaded):
    cheapest = Station.objects.cheapest_first().first()
    dearest = Station.objects.order_by("-retail_price").first()

    assert Decimal("2.00") < cheapest.retail_price < Decimal("4.00")
    assert Decimal("4.00") < dearest.retail_price < Decimal("8.00")


@pytest.mark.django_db
def test_a_known_station_survives_the_pipeline_intact(stations_loaded):
    """Row two of the supplied file, end to end."""
    station = Station.objects.get(opis_id=7)

    assert station.name == "WOODSHED OF BIG CABIN"
    assert (station.city, station.state) == ("Big Cabin", "OK")
    assert station.retail_price == Decimal("3.00733")
    assert station.geocode_precision == GeocodePrecision.CITY
    assert station.latitude == pytest.approx(36.5379, abs=0.01)
    assert station.longitude == pytest.approx(-95.2214, abs=0.01)


@pytest.mark.django_db
def test_loading_twice_is_idempotent(stations_loaded):
    from django.core.management import call_command

    call_command("load_stations", verbosity=0)
    assert Station.objects.count() == 6626


# --------------------------------------------------------------------------
# Integration with P0
# --------------------------------------------------------------------------


@pytest.mark.django_db
def test_health_now_reports_the_loaded_stations(client, stations_loaded):
    """The P0 probe reported zero; with P1 loaded it must report the truth."""
    body = client.get("/api/v1/health/").json()

    assert body["status"] == "ok"
    assert body["checks"]["stations_loaded"] == 6626
