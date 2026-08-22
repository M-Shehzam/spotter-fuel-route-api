"""Find the truckstops that sit along a route.

A transcontinental route arrives as tens of thousands of shape points, and
there are 6,623 geocoded truckstops. Comparing every station against every
point would be hundreds of millions of distance calculations per request.

Two reductions avoid that:

1. **Downsampling.** Shape points exist to draw a smooth line, not to measure
   proximity. Thinning the route to roughly one point every two miles loses
   nothing at a ten-mile corridor width and cuts the point count by an order
   of magnitude.

2. **Grid bucketing.** Stations and route points are dropped into half-degree
   cells once. A station can only be near the route if it shares a cell with a
   route point or neighbours one, so comparisons happen cell by cell against a
   handful of local points instead of the whole polyline.

The result is a few hundred thousand cheap operations rather than hundreds of
millions of expensive ones.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import numpy as np
from django.conf import settings

from apps.routing.geo import cumulative_miles, haversine_miles, planar_miles
from apps.routing.providers import RouteResult

logger = logging.getLogger(__name__)

# Half a degree of latitude is about 34.5 miles, wider than the default
# corridor, so one ring of neighbouring cells covers it.
CELL_DEGREES = 0.5

# Route thinning target. Two miles is far below the corridor width, so the
# nearest retained point is never meaningfully further away than the nearest
# original one.
ROUTE_SAMPLE_MILES = 2.0

Cell = tuple[int, int]


def _cell_of(latitude: float, longitude: float) -> Cell:
    return (int(math.floor(latitude / CELL_DEGREES)), int(math.floor(longitude / CELL_DEGREES)))


@dataclass(slots=True)
class Candidate:
    """A truckstop close enough to the route to be worth fuelling at."""

    opis_id: int
    price: float
    latitude: float
    longitude: float
    distance_along_route_miles: float
    detour_miles: float


@dataclass(slots=True)
class RouteGeometry:
    """A route thinned for proximity work, with mile markers retained.

    The full-resolution arrays are kept alongside the samples. Bucketing runs
    against the thinned points for speed, then the few hundred stations that
    survive are re-measured against the original geometry, so the reported
    detour and mile marker are exact rather than sampling artefacts.
    """

    latitudes: np.ndarray
    longitudes: np.ndarray
    mile_markers: np.ndarray
    total_miles: float
    original_point_count: int

    full_latitudes: np.ndarray
    full_longitudes: np.ndarray
    full_mile_markers: np.ndarray
    source_indices: np.ndarray

    @property
    def sample_count(self) -> int:
        return int(self.latitudes.size)


def prepare_route(result: RouteResult) -> RouteGeometry:
    """Measure and thin a provider's geometry.

    Mile markers are rescaled so the final one equals the provider's reported
    distance. Summing great-circle hops across a polyline slightly undershoots
    real road length, and letting the two disagree would make the fuel total
    inconsistent with the distance we report.
    """
    points = np.asarray(result.coordinates, dtype=np.float64)
    latitudes = points[:, 0]
    longitudes = points[:, 1]

    measured = cumulative_miles(latitudes, longitudes)
    travelled = float(measured[-1])
    if travelled > 0 and result.distance_miles > 0:
        measured *= result.distance_miles / travelled

    keep = _thin(measured)
    return RouteGeometry(
        latitudes=np.ascontiguousarray(latitudes[keep]),
        longitudes=np.ascontiguousarray(longitudes[keep]),
        mile_markers=np.ascontiguousarray(measured[keep]),
        total_miles=float(result.distance_miles or measured[-1]),
        original_point_count=int(latitudes.size),
        full_latitudes=latitudes,
        full_longitudes=longitudes,
        full_mile_markers=measured,
        source_indices=keep,
    )


def _thin(mile_markers: np.ndarray) -> np.ndarray:
    """Indices of points spaced about ``ROUTE_SAMPLE_MILES`` apart.

    The first and last points are always kept: they are the origin and the
    destination, and dropping either would distort both ends of the corridor.
    """
    if mile_markers.size <= 2:
        return np.arange(mile_markers.size)

    total = float(mile_markers[-1])
    wanted = np.arange(0.0, total, ROUTE_SAMPLE_MILES)
    keep = np.searchsorted(mile_markers, wanted, side="left")
    keep = np.unique(np.append(keep, mile_markers.size - 1))
    return keep[keep < mile_markers.size]


class StationIndex:
    """Geocoded stations as flat arrays plus a grid of cell memberships.

    Built once per process. 6,623 stations occupy well under a megabyte, so
    keeping them resident removes the database from the request path entirely.
    """

    def __init__(
        self,
        opis_ids: np.ndarray,
        latitudes: np.ndarray,
        longitudes: np.ndarray,
        prices: np.ndarray,
    ) -> None:
        self.opis_ids = opis_ids
        self.latitudes = latitudes
        self.longitudes = longitudes
        self.prices = prices
        self.cells = self._bucket(latitudes, longitudes)

    @staticmethod
    def _bucket(latitudes: np.ndarray, longitudes: np.ndarray) -> dict[Cell, np.ndarray]:
        rows = np.floor(latitudes / CELL_DEGREES).astype(np.int32)
        columns = np.floor(longitudes / CELL_DEGREES).astype(np.int32)

        grouped: dict[Cell, list[int]] = {}
        for position, cell in enumerate(zip(rows.tolist(), columns.tolist())):
            grouped.setdefault(cell, []).append(position)

        return {cell: np.asarray(members, dtype=np.int64) for cell, members in grouped.items()}

    @classmethod
    def from_database(cls) -> "StationIndex":
        from apps.stations.models import Station

        rows = list(
            Station.objects.geocoded()
            .values_list("opis_id", "latitude", "longitude", "retail_price")
        )
        if not rows:
            empty_int = np.empty(0, dtype=np.int64)
            empty_float = np.empty(0, dtype=np.float64)
            return cls(empty_int, empty_float, empty_float, empty_float)

        opis_ids = np.fromiter((row[0] for row in rows), dtype=np.int64, count=len(rows))
        latitudes = np.fromiter((row[1] for row in rows), dtype=np.float64, count=len(rows))
        longitudes = np.fromiter((row[2] for row in rows), dtype=np.float64, count=len(rows))
        prices = np.fromiter((float(row[3]) for row in rows), dtype=np.float64, count=len(rows))
        return cls(opis_ids, latitudes, longitudes, prices)

    def __len__(self) -> int:
        return int(self.opis_ids.size)


_index: StationIndex | None = None


def get_station_index() -> StationIndex:
    """The process-wide station index, built on first use."""
    global _index
    if _index is None:
        began = time.perf_counter()
        _index = StationIndex.from_database()
        logger.info(
            "Station index built: %d stations, %d cells, %.1f ms",
            len(_index),
            len(_index.cells),
            (time.perf_counter() - began) * 1000,
        )
    return _index


def reset_station_index() -> None:
    """Drop the cached index. Used after loading data and by tests."""
    global _index
    _index = None


def find_candidates(
    route: RouteGeometry,
    max_detour_miles: float | None = None,
    index: StationIndex | None = None,
) -> list[Candidate]:
    """Stations within ``max_detour_miles`` of the route, ordered by mile marker.

    Each candidate carries how far along the route it sits, which is what the
    optimizer needs to reason about tank range.
    """
    detour_limit = (
        settings.MAX_DETOUR_MILES if max_detour_miles is None else float(max_detour_miles)
    )
    # An index holding no stations is falsy, so this tests for None. Testing
    # truthiness would send an empty index to the database instead.
    if index is None:
        index = get_station_index()
    if len(index) == 0 or route.sample_count == 0:
        return []

    route_cells = _bucket_route(route)
    ring = _ring_radius(detour_limit)

    best_distance: dict[int, float] = {}
    best_sample: dict[int, int] = {}

    for cell, station_positions in index.cells.items():
        nearby = _gather(route_cells, cell, ring)
        if nearby is None:
            continue

        station_lats = index.latitudes[station_positions]
        station_lons = index.longitudes[station_positions]
        point_lats = route.latitudes[nearby]
        point_lons = route.longitudes[nearby]

        # One cosine for the whole block: cells are half a degree tall, so a
        # single reference latitude is representative throughout.
        cos_reference = math.cos(math.radians((cell[0] + 0.5) * CELL_DEGREES))

        distances = planar_miles(
            station_lats[:, None],
            station_lons[:, None],
            point_lats[None, :],
            point_lons[None, :],
            cos_reference,
        )

        nearest = np.argmin(distances, axis=1)
        closest = distances[np.arange(distances.shape[0]), nearest]

        within = np.flatnonzero(closest <= detour_limit)
        for offset in within.tolist():
            position = int(station_positions[offset])
            distance = float(closest[offset])
            if distance < best_distance.get(position, math.inf):
                best_distance[position] = distance
                # Route cells are built from the thinned arrays, so this is
                # already a sample index.
                best_sample[position] = int(nearby[nearest[offset]])

    candidates = []
    for position, sample in best_sample.items():
        detour_miles, marker_miles = _refine(route, index, position, sample)
        if detour_miles > detour_limit:
            # The exact measurement can push a borderline station out; trust it
            # over the sampled estimate that let it in.
            continue
        candidates.append(
            Candidate(
                opis_id=int(index.opis_ids[position]),
                price=float(index.prices[position]),
                latitude=float(index.latitudes[position]),
                longitude=float(index.longitudes[position]),
                distance_along_route_miles=marker_miles,
                detour_miles=detour_miles,
            )
        )
    candidates.sort(key=lambda candidate: (candidate.distance_along_route_miles, candidate.price))
    return candidates


def _refine(
    route: RouteGeometry, index: StationIndex, position: int, sample: int
) -> tuple[float, float]:
    """Re-measure one station against the full-resolution geometry.

    Bucketing works on thinned points, so its nearest match can be off by up to
    half the sample spacing. Only a few hundred stations survive the filter, so
    scanning the original points between the neighbouring samples costs little
    and makes the reported detour and mile marker exact.
    """
    last_sample = route.source_indices.size - 1
    low = int(route.source_indices[max(0, sample - 1)])
    high = int(route.source_indices[min(last_sample, sample + 1)])
    if high <= low:
        high = min(low + 1, route.full_latitudes.size - 1)

    window_lats = route.full_latitudes[low : high + 1]
    window_lons = route.full_longitudes[low : high + 1]

    distances = haversine_miles(
        index.latitudes[position],
        index.longitudes[position],
        window_lats,
        window_lons,
    )
    nearest = int(np.argmin(distances))
    return float(distances[nearest]), float(route.full_mile_markers[low + nearest])


def _bucket_route(route: RouteGeometry) -> dict[Cell, np.ndarray]:
    rows = np.floor(route.latitudes / CELL_DEGREES).astype(np.int32)
    columns = np.floor(route.longitudes / CELL_DEGREES).astype(np.int32)

    grouped: dict[Cell, list[int]] = {}
    for position, cell in enumerate(zip(rows.tolist(), columns.tolist())):
        grouped.setdefault(cell, []).append(position)

    return {cell: np.asarray(members, dtype=np.int64) for cell, members in grouped.items()}


def _ring_radius(detour_miles: float) -> int:
    """How many cells out to look, given the corridor width.

    Longitude degrees shrink towards the poles, so the northern edge of the
    contiguous states sets the worst case.
    """
    miles_per_cell = CELL_DEGREES * 69.0 * math.cos(math.radians(49.0))
    return max(1, math.ceil(detour_miles / miles_per_cell))


def _gather(route_cells: dict[Cell, np.ndarray], cell: Cell, ring: int) -> np.ndarray | None:
    """Route point indices in ``cell`` and its neighbours, or None if there are none."""
    row, column = cell
    blocks = [
        route_cells[(row + dr, column + dc)]
        for dr in range(-ring, ring + 1)
        for dc in range(-ring, ring + 1)
        if (row + dr, column + dc) in route_cells
    ]
    if not blocks:
        return None
    return blocks[0] if len(blocks) == 1 else np.concatenate(blocks)
