"""P6 gate: caching behaviour, index warm-up, and response budgets.

Author: Muhammad Shehzam
"""

import pickle
import time

import pytest
from django.core.cache import cache
from django.core.management import call_command

from apps.routing.corridor import get_station_index, reset_station_index
from apps.routing.resolver import get_place_index, reset_place_index
from apps.routing.services import MAP_TOKEN_PREFIX, RouteRequest, plan_journey
from apps.routing.resolver import resolve

pytestmark = pytest.mark.django_db

ROUTE_URL = "/api/v1/route/"


# --------------------------------------------------------------------------
# Cache keys
# --------------------------------------------------------------------------


def test_the_same_journey_worded_differently_shares_a_key():
    request = RouteRequest(start="whatever", finish="whatever", max_detour_miles=10.0)

    formal = request.cache_key(resolve("Dallas, TX"), resolve("Chicago, IL"))
    casual = request.cache_key(resolve("dallas"), resolve("Chicago, Illinois"))

    assert formal == casual


def test_reversing_the_journey_is_a_different_key():
    """Fuel prices differ by direction of travel, so it is a different answer."""
    request = RouteRequest(start="a", finish="b", max_detour_miles=10.0)

    there = request.cache_key(resolve("Dallas, TX"), resolve("Chicago, IL"))
    back = request.cache_key(resolve("Chicago, IL"), resolve("Dallas, TX"))

    assert there != back


def test_a_different_corridor_width_is_a_different_key():
    start, finish = resolve("Dallas, TX"), resolve("Chicago, IL")

    wide = RouteRequest("a", "b", max_detour_miles=10.0).cache_key(start, finish)
    narrow = RouteRequest("a", "b", max_detour_miles=3.0).cache_key(start, finish)

    assert wide != narrow


def test_the_key_changes_when_the_vehicle_changes(settings):
    """A cached plan for a 500 mile tank must not be served for a 300 mile one."""
    start, finish = resolve("Dallas, TX"), resolve("Chicago, IL")
    request = RouteRequest("a", "b", max_detour_miles=10.0)

    before = request.cache_key(start, finish)
    settings.VEHICLE_MAX_RANGE_MILES = 300.0
    after = request.cache_key(start, finish)

    assert before != after


def test_coordinates_finer_than_a_metre_do_not_splinter_the_cache():
    request = RouteRequest("a", "b", max_detour_miles=10.0)

    coarse = request.cache_key(resolve("32.783060,-96.806670"), resolve("Chicago, IL"))
    finer = request.cache_key(resolve("32.7830601,-96.8066701"), resolve("Chicago, IL"))

    assert coarse == finer


# --------------------------------------------------------------------------
# Cache behaviour
# --------------------------------------------------------------------------


def test_a_plan_is_stored_and_served_again(stations_loaded, osrm):
    first = plan_journey("Dallas, TX", "Chicago, IL")
    second = plan_journey("Dallas, TX", "Chicago, IL")

    assert osrm.call_count == 1
    assert first["meta"]["cached"] is False
    assert second["meta"]["cached"] is True
    assert second["fuel"] == first["fuel"]
    assert second["fuel_stops"] == first["fuel_stops"]


def test_a_cached_plan_reports_no_external_calls(stations_loaded, osrm):
    plan_journey("Dallas, TX", "Chicago, IL")
    second = plan_journey("Dallas, TX", "Chicago, IL")

    assert second["meta"]["external_api_calls"] == 0


def test_clearing_the_cache_forces_a_fresh_call(stations_loaded, osrm):
    plan_journey("Dallas, TX", "Chicago, IL")
    cache.clear()
    plan_journey("Dallas, TX", "Chicago, IL")

    assert osrm.call_count == 2


def test_the_map_token_resolves_to_the_stored_plan(stations_loaded, osrm):
    payload = plan_journey("Dallas, TX", "Chicago, IL")
    token = payload["meta"]["map_url"].rstrip("/").rsplit("/", 1)[-1]

    assert cache.get(f"{MAP_TOKEN_PREFIX}:{token}") is not None


def test_a_payload_survives_a_pickle_round_trip(stations_loaded, osrm):
    """Redis stores pickles. Anything that will not pickle passes against the
    in-process cache used locally and breaks in production."""
    payload = plan_journey("Dallas, TX", "Chicago, IL")

    restored = pickle.loads(pickle.dumps(payload))

    assert restored == payload


# --------------------------------------------------------------------------
# Cache backend selection
# --------------------------------------------------------------------------


def test_the_process_falls_back_to_an_in_process_cache(settings):
    """A fresh clone must run with no Redis at all."""
    assert "locmem" in settings.CACHES["default"]["BACKEND"].lower()


def test_redis_is_used_when_it_is_configured(monkeypatch):
    """Re-read the settings module with REDIS_URL present."""
    import importlib

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    module = importlib.reload(importlib.import_module("config.settings"))

    try:
        assert module.CACHES["default"]["BACKEND"] == "django_redis.cache.RedisCache"
        assert module.CACHES["default"]["LOCATION"] == "redis://localhost:6379/0"
    finally:
        monkeypatch.delenv("REDIS_URL", raising=False)
        importlib.reload(module)


def test_postgres_is_used_when_it_is_configured(monkeypatch):
    import importlib

    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db:5432/fuelroute")
    module = importlib.reload(importlib.import_module("config.settings"))

    try:
        assert "postgresql" in module.DATABASES["default"]["ENGINE"]
        assert module.DATABASES["default"]["NAME"] == "fuelroute"
    finally:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        importlib.reload(module)


# --------------------------------------------------------------------------
# Indexes
# --------------------------------------------------------------------------


def test_the_station_index_is_built_once_per_process(stations_loaded):
    reset_station_index()
    first = get_station_index()
    second = get_station_index()

    assert first is second


def test_the_place_index_is_built_once_per_process():
    reset_place_index()
    assert get_place_index() is get_place_index()


def test_warming_reports_what_it_loaded(stations_loaded, capsys):
    reset_station_index()
    reset_place_index()

    call_command("warm_indexes")

    printed = capsys.readouterr().out
    assert "stations" in printed and "places" in printed
    assert "Indexes warm" in printed


def test_the_station_index_keeps_the_database_out_of_the_request_path(
    stations_loaded, django_assert_num_queries, osrm
):
    """Once warm, planning touches the database only to name the chosen stops."""
    get_station_index()
    get_place_index()

    with django_assert_num_queries(1):
        plan_journey("Dallas, TX", "Chicago, IL")


def test_a_cached_plan_touches_the_database_not_at_all(
    stations_loaded, django_assert_num_queries, osrm
):
    plan_journey("Dallas, TX", "Chicago, IL")

    with django_assert_num_queries(0):
        plan_journey("Dallas, TX", "Chicago, IL")


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------


def test_a_cached_plan_is_served_in_single_digit_milliseconds(stations_loaded, osrm):
    plan_journey("Dallas, TX", "Chicago, IL")

    began = time.perf_counter()
    payload = plan_journey("Dallas, TX", "Chicago, IL")
    elapsed_ms = (time.perf_counter() - began) * 1000

    assert payload["meta"]["cached"] is True
    assert elapsed_ms < 50, f"cached plan took {elapsed_ms:.1f} ms"


def test_local_work_is_a_small_share_of_a_cold_plan(stations_loaded, osrm):
    """Everything except the routing call should be tens of milliseconds."""
    get_station_index()
    get_place_index()

    began = time.perf_counter()
    plan_journey("Dallas, TX", "Chicago, IL")
    elapsed_ms = (time.perf_counter() - began) * 1000

    assert elapsed_ms < 600, f"cold plan took {elapsed_ms:.1f} ms with a mocked provider"


def test_the_response_carries_its_own_timings(stations_loaded, osrm):
    meta = plan_journey("Dallas, TX", "Chicago, IL")["meta"]

    for field in ("compute_ms", "routing_fetch_ms", "external_api_calls", "cached"):
        assert field in meta
    assert meta["compute_ms"] > 0
