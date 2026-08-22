"""Orchestration: from two place names to a costed fuelling plan.

This is where the brief's headline constraint is kept. Resolving the endpoints
uses the committed gazetteer, corridor matching and optimisation run against
resident data, and the only thing that leaves the process is a single routing
request. A repeat of the same journey serves from cache and leaves the process
not at all.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from apps.routing.corridor import find_candidates, prepare_route
from apps.routing.optimizer import FuelPlan, plan_fuel_stops
from apps.routing.polyline import encode
from apps.routing.providers import Coordinate, RouteResult, fetch_route
from apps.routing.resolver import ResolvedLocation, resolve
from apps.stations.models import Station

CACHE_VERSION = "v1"
MAP_TOKEN_PREFIX = "map"


@dataclass(slots=True)
class RouteRequest:
    start: str
    finish: str
    max_detour_miles: float

    def cache_key(self, start: ResolvedLocation, finish: ResolvedLocation) -> str:
        """Key on resolved coordinates, not the raw text.

        "Dallas, TX" and "dallas" describe the same journey and should share a
        cached answer. Rounding to five decimals keeps coordinate input from
        splintering the cache over differences finer than a metre.
        """
        material = (
            f"{start.latitude:.5f},{start.longitude:.5f}|"
            f"{finish.latitude:.5f},{finish.longitude:.5f}|"
            f"{self.max_detour_miles:.2f}|"
            f"{settings.VEHICLE_MAX_RANGE_MILES:.1f}|{settings.VEHICLE_MPG:.2f}|"
            f"{settings.ORIGIN_FUEL_RADIUS_MILES:.1f}"
        )
        digest = hashlib.sha256(material.encode()).hexdigest()[:32]
        return f"route:{CACHE_VERSION}:{digest}"


def plan_journey(
    start_text: str,
    finish_text: str,
    max_detour_miles: float | None = None,
) -> dict:
    """Resolve, route, match and optimise. Returns the API payload."""
    began = time.perf_counter()

    detour = (
        settings.MAX_DETOUR_MILES if max_detour_miles is None else float(max_detour_miles)
    )
    request = RouteRequest(start=start_text, finish=finish_text, max_detour_miles=detour)

    # No external call: the gazetteer ships with the repo.
    start = resolve(start_text)
    finish = resolve(finish_text)

    key = request.cache_key(start, finish)
    cached = cache.get(key)
    if cached is not None:
        payload = dict(cached)
        payload["meta"] = {
            **payload["meta"],
            "cached": True,
            "external_api_calls": 0,
            "compute_ms": round((time.perf_counter() - began) * 1000, 2),
        }
        return payload

    # The one external call.
    route = fetch_route(
        Coordinate(start.latitude, start.longitude),
        Coordinate(finish.latitude, finish.longitude),
    )

    geometry = prepare_route(route)
    candidates = find_candidates(geometry, max_detour_miles=detour)
    plan = plan_fuel_stops(
        candidates,
        geometry.total_miles,
        tank_range_miles=settings.VEHICLE_MAX_RANGE_MILES,
        mpg=settings.VEHICLE_MPG,
        origin_radius_miles=settings.ORIGIN_FUEL_RADIUS_MILES,
    )

    token = key.rsplit(":", 1)[-1]
    payload = _build_payload(request, start, finish, route, geometry, plan, token)
    payload["meta"]["compute_ms"] = round((time.perf_counter() - began) * 1000, 2)

    cache.set(key, payload, settings.ROUTE_CACHE_TTL_SECONDS)
    cache.set(f"{MAP_TOKEN_PREFIX}:{token}", key, settings.ROUTE_CACHE_TTL_SECONDS)

    return payload


def payload_for_token(token: str) -> dict | None:
    """Recover a previously planned journey, for the map view."""
    key = cache.get(f"{MAP_TOKEN_PREFIX}:{token}")
    return cache.get(key) if key else None


def _build_payload(
    request: RouteRequest,
    start: ResolvedLocation,
    finish: ResolvedLocation,
    route: RouteResult,
    geometry,
    plan: FuelPlan,
    token: str,
) -> dict:
    stations = _stations_for(plan)

    thinned = [
        (float(lat), float(lon))
        for lat, lon in zip(geometry.latitudes, geometry.longitudes)
    ]

    return {
        "request": {
            "start": {"query": start.query, "resolved": start.label, "source": start.source,
                      "latitude": round(start.latitude, 6), "longitude": round(start.longitude, 6)},
            "finish": {"query": finish.query, "resolved": finish.label, "source": finish.source,
                       "latitude": round(finish.latitude, 6), "longitude": round(finish.longitude, 6)},
            "max_detour_miles": request.max_detour_miles,
        },
        "vehicle": {
            "max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
            "mpg": settings.VEHICLE_MPG,
            "tank_gallons": round(settings.VEHICLE_MAX_RANGE_MILES / settings.VEHICLE_MPG, 2),
        },
        "route": {
            "total_distance_miles": round(geometry.total_miles, 2),
            "total_duration_hours": round(route.duration_hours, 2),
            "provider": route.provider,
            "geometry": {
                "type": "LineString",
                "coordinates": [[round(lon, 5), round(lat, 5)] for lat, lon in thinned],
            },
            "polyline": encode(thinned, precision=5),
            "bbox": [round(value, 5) for value in route.bbox()],
            "shape_points": geometry.original_point_count,
            "simplified_points": len(thinned),
        },
        "fuel": {
            "feasible": plan.feasible,
            "total_gallons": round(plan.total_gallons, 3),
            "total_cost_usd": round(plan.total_cost, 2),
            "average_price_per_gallon": round(plan.average_price_per_gallon, 4),
            "stops_count": plan.stop_count,
            "naive_cost_usd": round(plan.naive_cost, 2),
            "savings_usd": round(plan.savings, 2),
            "savings_percent": (
                round(plan.savings / plan.naive_cost * 100, 2) if plan.naive_cost else 0.0
            ),
            "infeasible_reason": plan.infeasible_reason,
        },
        "fuel_stops": [
            {
                "sequence": stop.sequence,
                "opis_id": stop.candidate.opis_id,
                "name": stations.get(stop.candidate.opis_id, {}).get("name", ""),
                "address": stations.get(stop.candidate.opis_id, {}).get("address", ""),
                "city": stations.get(stop.candidate.opis_id, {}).get("city", ""),
                "state": stations.get(stop.candidate.opis_id, {}).get("state", ""),
                "latitude": round(stop.candidate.latitude, 6),
                "longitude": round(stop.candidate.longitude, 6),
                "price_per_gallon": round(stop.candidate.price, 4),
                "gallons": round(stop.gallons, 3),
                "cost_usd": round(stop.cost, 2),
                "distance_from_start_miles": round(stop.mile_marker, 2),
                "detour_miles": round(stop.candidate.detour_miles, 2),
                "range_on_arrival_miles": round(stop.arrival_range_miles, 1),
                "range_on_departure_miles": round(stop.departure_range_miles, 1),
            }
            for stop in plan.stops
        ],
        "meta": {
            "external_api_calls": route.api_calls,
            "cached": False,
            "routing_fetch_ms": round(route.fetch_ms, 2),
            "candidates_considered": plan.candidates_considered,
            "largest_gap_miles": round(plan.largest_gap_miles, 2),
            "map_url": f"/api/v1/route/map/{token}/",
            "compute_ms": 0.0,
        },
    }


def _stations_for(plan: FuelPlan) -> dict[int, dict]:
    """One query for every stop's descriptive fields."""
    ids = [stop.candidate.opis_id for stop in plan.stops]
    if not ids:
        return {}

    rows = Station.objects.filter(opis_id__in=ids).values(
        "opis_id", "name", "address", "city", "state"
    )
    return {row["opis_id"]: row for row in rows}
