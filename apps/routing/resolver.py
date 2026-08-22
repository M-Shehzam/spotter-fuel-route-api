"""Turn what a user types into coordinates, without calling anything.

The brief allows one external call per request and the route itself needs it,
so endpoint resolution runs entirely against the gazetteer committed in
``data/us_places.csv``. Raw coordinates are accepted too, for callers that
already have them.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass

from django.conf import settings

from apps.stations.geocoding import normalize_city

logger = logging.getLogger(__name__)

COORDINATE_PAIR = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*[,/ ]\s*(-?\d+(?:\.\d+)?)\s*$"
)

STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME",
    "MARYLAND": "MD", "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}
STATE_CODES = frozenset(STATE_NAMES.values())

# Roughly the bounding box of the United States including Alaska and Hawaii.
US_BOUNDS = (18.0, 72.0, -180.0, -64.0)


class LocationNotFound(Exception):
    """The text could not be matched to a place in the USA."""


@dataclass(slots=True)
class ResolvedLocation:
    latitude: float
    longitude: float
    label: str
    source: str  # "coordinates" or "gazetteer"
    query: str = ""


class PlaceIndex:
    """The committed gazetteer, keyed by folded name and state."""

    def __init__(self) -> None:
        self._by_name_state: dict[tuple[str, str], tuple[float, float, int, str]] = {}
        self._by_name: dict[str, tuple[float, float, int, str]] = {}

    @classmethod
    def load(cls, path=None) -> "PlaceIndex":
        path = path or settings.PLACES_CSV
        index = cls()

        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                state = row["state"].strip().upper()
                key = normalize_city(row["name"])
                if not key or not state:
                    continue

                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
                population = int(row["population"] or 0)
                label = f"{row['name'].strip()}, {state}"
                entry = (latitude, longitude, population, label)

                existing = index._by_name_state.get((key, state))
                if existing is None or population > existing[2]:
                    index._by_name_state[(key, state)] = entry

                # A bare city name resolves to the largest place of that name,
                # which is what someone typing "Springfield" almost always means.
                bare = index._by_name.get(key)
                if bare is None or population > bare[2]:
                    index._by_name[key] = entry

        return index

    def lookup(self, city: str, state: str | None) -> tuple[float, float, str] | None:
        key = normalize_city(city)
        if not key:
            return None

        if state:
            entry = self._by_name_state.get((key, state.upper()))
        else:
            entry = self._by_name.get(key)

        return (entry[0], entry[1], entry[3]) if entry else None

    def __len__(self) -> int:
        return len(self._by_name_state)


_places: PlaceIndex | None = None


def get_place_index() -> PlaceIndex:
    global _places
    if _places is None:
        _places = PlaceIndex.load()
        logger.info("Place index loaded: %d entries", len(_places))
    return _places


def reset_place_index() -> None:
    global _places
    _places = None


def resolve(text: str, *, index: PlaceIndex | None = None) -> ResolvedLocation:
    """Resolve free text or a coordinate pair to a location inside the USA.

    Accepts ``"32.7767,-96.7970"``, ``"Dallas, TX"``, ``"Dallas, Texas"`` or a
    bare ``"Dallas"``, which resolves to the largest place of that name.
    """
    raw = (text or "").strip()
    if not raw:
        raise LocationNotFound("A start and a finish are both required.")

    coordinates = COORDINATE_PAIR.match(raw)
    if coordinates:
        latitude = float(coordinates.group(1))
        longitude = float(coordinates.group(2))
        _require_us(latitude, longitude, raw)
        return ResolvedLocation(
            latitude=latitude,
            longitude=longitude,
            label=f"{latitude:.5f}, {longitude:.5f}",
            source="coordinates",
            query=raw,
        )

    index = index or get_place_index()
    city, state = _split(raw)
    hit = index.lookup(city, state)

    if hit is None and state is None:
        # "Dallas TX" without the comma.
        words = raw.split()
        if len(words) > 1 and words[-1].upper() in STATE_CODES:
            hit = index.lookup(" ".join(words[:-1]), words[-1].upper())

    if hit is None:
        raise LocationNotFound(
            f"Could not place {raw!r} in the USA. Try a city and state such as "
            f"'Dallas, TX', or a latitude and longitude such as '32.7767,-96.7970'."
        )

    latitude, longitude, label = hit
    return ResolvedLocation(
        latitude=latitude,
        longitude=longitude,
        label=label,
        source="gazetteer",
        query=raw,
    )


def _split(raw: str) -> tuple[str, str | None]:
    """Separate a trailing state from a city name."""
    if "," not in raw:
        return raw, None

    city, _, tail = raw.rpartition(",")
    tail = tail.strip().upper()

    if tail in STATE_CODES:
        return city.strip(), tail
    if tail in STATE_NAMES:
        return city.strip(), STATE_NAMES[tail]

    # Something like "Dallas, Texas, USA": drop the country and try again.
    if tail in {"USA", "US", "UNITED STATES"}:
        return _split(city.strip())

    return raw, None


def _require_us(latitude: float, longitude: float, raw: str) -> None:
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise LocationNotFound(f"{raw!r} is not a valid latitude and longitude.")

    south, north, west, east = US_BOUNDS
    if not (south <= latitude <= north and west <= longitude <= east):
        raise LocationNotFound(
            f"{latitude:.4f}, {longitude:.4f} is outside the USA. "
            "The brief scopes this service to routes within the United States."
        )
