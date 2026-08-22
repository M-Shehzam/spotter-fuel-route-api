"""Routing providers.

The brief asks that the service lean on the external routing API as little as
possible, so the contract here is deliberately narrow: one request yields the
whole route geometry, and everything downstream (corridor search, stop
selection) runs locally against that single response.

OSRM's demo server is the default because it needs no API key, which keeps a
fresh clone runnable without credentials. Public demo servers do go down, so a
fallback provider can be named in the environment; it is consulted only after
the primary has actually failed, leaving the healthy path at exactly one call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from django.conf import settings

from apps.routing.polyline import decode

logger = logging.getLogger(__name__)

METRES_PER_MILE = 1609.344


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class RoutingError(Exception):
    """Base class for every routing failure."""


class RouteNotFound(RoutingError):
    """The provider answered, but no drivable route connects the points."""


class RoutingUnavailable(RoutingError):
    """The provider could not be reached, timed out, or returned a 5xx."""


class RoutingRequestInvalid(RoutingError):
    """The provider rejected the request, usually bad coordinates."""


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RouteResult:
    """A single driving route, already in the units the rest of the app uses."""

    coordinates: list[tuple[float, float]]
    distance_miles: float
    duration_hours: float
    provider: str
    api_calls: int = 1
    fetch_ms: float = 0.0

    @property
    def point_count(self) -> int:
        return len(self.coordinates)

    def as_geojson(self) -> dict:
        """GeoJSON wants (longitude, latitude); we carry (latitude, longitude)."""
        return {
            "type": "LineString",
            "coordinates": [[round(lon, 6), round(lat, 6)] for lat, lon in self.coordinates],
        }

    def bbox(self) -> list[float]:
        """``[west, south, east, north]`` for fitting a map viewport."""
        lats = [lat for lat, _ in self.coordinates]
        lons = [lon for _, lon in self.coordinates]
        return [min(lons), min(lats), max(lons), max(lats)]


@dataclass(slots=True)
class Coordinate:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError(f"Latitude out of range: {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError(f"Longitude out of range: {self.longitude}")


# --------------------------------------------------------------------------
# Provider protocol
# --------------------------------------------------------------------------


class RoutingProvider(Protocol):
    name: str

    def route(self, start: Coordinate, finish: Coordinate) -> RouteResult: ...


_client: httpx.Client | None = None


def _shared_client() -> httpx.Client:
    """One pooled client per process, so repeat requests skip the TLS handshake."""
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=httpx.Timeout(settings.ROUTING_TIMEOUT_SECONDS, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            headers={"User-Agent": "spotter-fuel-route-api/1.0"},
            follow_redirects=True,
        )
    return _client


def reset_client() -> None:
    """Drop the pooled client. Used by tests."""
    global _client
    if _client is not None:
        _client.close()
    _client = None


# --------------------------------------------------------------------------
# OSRM
# --------------------------------------------------------------------------


@dataclass(slots=True)
class OSRMProvider:
    """Open Source Routing Machine. No API key required.

    Asks for ``overview=full`` so the geometry carries enough shape points for
    accurate corridor matching, and ``polyline6`` to keep the payload small.
    """

    name: str = "osrm"
    base_url: str = field(default_factory=lambda: settings.OSRM_BASE_URL)

    def route(self, start: Coordinate, finish: Coordinate) -> RouteResult:
        url = (
            f"{self.base_url.rstrip('/')}/route/v1/driving/"
            f"{start.longitude},{start.latitude};{finish.longitude},{finish.latitude}"
        )
        params = {
            "overview": "full",
            "geometries": "polyline6",
            "steps": "false",
            "alternatives": "false",
        }

        began = time.perf_counter()
        try:
            response = _shared_client().get(url, params=params)
        except httpx.TimeoutException as exc:
            raise RoutingUnavailable(f"OSRM timed out after {settings.ROUTING_TIMEOUT_SECONDS}s") from exc
        except httpx.HTTPError as exc:
            raise RoutingUnavailable(f"OSRM unreachable: {exc}") from exc
        elapsed_ms = (time.perf_counter() - began) * 1000

        if response.status_code >= 500:
            raise RoutingUnavailable(f"OSRM returned {response.status_code}")
        if response.status_code == 429:
            raise RoutingUnavailable("OSRM rate limited this client (429)")

        try:
            payload = response.json()
        except ValueError as exc:
            raise RoutingUnavailable("OSRM returned a non-JSON body") from exc

        code = payload.get("code", "")
        if code in {"NoRoute", "NoSegment", "NoTrips"}:
            raise RouteNotFound(
                "No drivable route connects those points. "
                "Both must be reachable by road within the USA."
            )
        if code != "Ok":
            message = payload.get("message", code or "unknown error")
            if response.status_code == 400:
                raise RoutingRequestInvalid(f"OSRM rejected the request: {message}")
            raise RoutingUnavailable(f"OSRM error: {message}")

        routes = payload.get("routes") or []
        if not routes:
            raise RouteNotFound("OSRM returned no routes")

        best = routes[0]
        coordinates = decode(best.get("geometry", ""), precision=6)
        if len(coordinates) < 2:
            raise RoutingUnavailable("OSRM returned a degenerate geometry")

        return RouteResult(
            coordinates=coordinates,
            distance_miles=float(best["distance"]) / METRES_PER_MILE,
            duration_hours=float(best["duration"]) / 3600.0,
            provider=self.name,
            api_calls=1,
            fetch_ms=elapsed_ms,
        )


# --------------------------------------------------------------------------
# Valhalla
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ValhallaProvider:
    """FOSSGIS Valhalla. No API key, and it can cost a route as a truck.

    Kept as a standby for when the OSRM demo server is unavailable.
    """

    name: str = "valhalla"
    base_url: str = field(default_factory=lambda: settings.VALHALLA_BASE_URL)

    def route(self, start: Coordinate, finish: Coordinate) -> RouteResult:
        url = f"{self.base_url.rstrip('/')}/route"
        body = {
            "locations": [
                {"lat": start.latitude, "lon": start.longitude},
                {"lat": finish.latitude, "lon": finish.longitude},
            ],
            "costing": "truck",
            "units": "miles",
            "directions_options": {"units": "miles"},
        }

        began = time.perf_counter()
        try:
            response = _shared_client().post(url, json=body)
        except httpx.TimeoutException as exc:
            raise RoutingUnavailable("Valhalla timed out") from exc
        except httpx.HTTPError as exc:
            raise RoutingUnavailable(f"Valhalla unreachable: {exc}") from exc
        elapsed_ms = (time.perf_counter() - began) * 1000

        if response.status_code >= 500:
            raise RoutingUnavailable(f"Valhalla returned {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise RoutingUnavailable("Valhalla returned a non-JSON body") from exc

        if response.status_code == 400 or "error" in payload:
            message = payload.get("error", "unknown error")
            # 442 is Valhalla's "no route between locations".
            if payload.get("error_code") in {442, 443}:
                raise RouteNotFound(str(message))
            raise RoutingRequestInvalid(f"Valhalla rejected the request: {message}")

        trip = payload.get("trip") or {}
        legs = trip.get("legs") or []
        if not legs:
            raise RouteNotFound("Valhalla returned no legs")

        coordinates: list[tuple[float, float]] = []
        for leg in legs:
            coordinates.extend(decode(leg.get("shape", ""), precision=6))
        if len(coordinates) < 2:
            raise RoutingUnavailable("Valhalla returned a degenerate geometry")

        summary = trip.get("summary") or {}
        return RouteResult(
            coordinates=coordinates,
            distance_miles=float(summary.get("length", 0.0)),
            duration_hours=float(summary.get("time", 0.0)) / 3600.0,
            provider=self.name,
            api_calls=1,
            fetch_ms=elapsed_ms,
        )


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

PROVIDERS: dict[str, type] = {
    "osrm": OSRMProvider,
    "valhalla": ValhallaProvider,
}


def get_provider(name: str | None = None) -> RoutingProvider:
    key = (name or settings.ROUTING_PROVIDER).lower()
    try:
        return PROVIDERS[key]()
    except KeyError:
        raise RoutingRequestInvalid(
            f"Unknown routing provider {key!r}. Choose one of: {', '.join(sorted(PROVIDERS))}"
        ) from None


def fetch_route(start: Coordinate, finish: Coordinate) -> RouteResult:
    """Fetch a route, falling back to the standby provider only on failure.

    A healthy request makes exactly one external call. The fallback fires only
    when the primary is unreachable or broken, never to improve a result, so
    the second call is a recovery path rather than routine behaviour.
    """
    primary = get_provider()
    try:
        return primary.route(start, finish)
    except (RouteNotFound, RoutingRequestInvalid):
        # A definitive answer. Asking a second provider would not change it.
        raise
    except RoutingUnavailable as exc:
        fallback_name = (settings.ROUTING_FALLBACK_PROVIDER or "").lower()
        if not fallback_name or fallback_name == primary.name:
            raise

        logger.warning("Routing provider %s unavailable (%s); trying %s", primary.name, exc, fallback_name)
        result = get_provider(fallback_name).route(start, finish)
        # Report both attempts so the response never understates its egress.
        result.api_calls = 2
        return result
