"""Offline geocoding for truckstops.

The price file gives a city and state but no coordinates, and its Address
column holds highway-exit descriptors ("I-44, EXIT 283 & US-69") that street
geocoders handle badly. So the baseline resolves city centroids from the
GeoNames US dump: no API key, no rate limit, and identical output on every
machine that clones the repo.

City-centroid error is roughly one to three miles. Against a 500-mile tank and
a ten-mile corridor that is immaterial, and P9 upgrades matched stations to
true POI coordinates.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

# GeoNames dump column offsets (the file has no header row).
COL_NAME = 1
COL_ASCII = 2
COL_ALTERNATES = 3
COL_LAT = 4
COL_LON = 5
COL_FEATURE_CLASS = 6
COL_FEATURE_CODE = 7
COL_ADMIN1 = 10
COL_POPULATION = 14

# Populated places only. Class P covers cities, towns and villages; the tiny
# settlements many truckstops sit in are PPL, so we cannot filter by population.
POPULATED_CLASS = "P"

# Codes marking places that no longer exist. A truckstop is never in one, and
# leaving them in lets a ghost town outrank the real city of the same name.
HISTORICAL_CODES = frozenset({"PPLQ", "PPLW", "PPLH", "PPLCH"})

# Apostrophes are deleted rather than blanked, so "O'Neill" folds to ONEILL
# instead of splitting into two words.
_APOSTROPHE = re.compile(r"[\u2019']")
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")

# Abbreviations the price file uses that GeoNames spells out.
_EXPANSIONS = {
    "ST": "SAINT",
    "STE": "SAINTE",
    "MT": "MOUNT",
    "FT": "FORT",
    "N": "NORTH",
    "S": "SOUTH",
    "E": "EAST",
    "W": "WEST",
    "NE": "NORTHEAST",
    "NW": "NORTHWEST",
    "SE": "SOUTHEAST",
    "SW": "SOUTHWEST",
    "HTS": "HEIGHTS",
    "JCT": "JUNCTION",
    "SPGS": "SPRINGS",
    "SPG": "SPRING",
    "CTR": "CENTER",
    "PT": "PORT",
}


def normalize_city(name: str) -> str:
    """Fold a city name to a comparable key.

    Uppercases, strips punctuation, then expands the directional and honorific
    abbreviations that differ between the two sources.
    """
    folded = _PUNCT.sub(" ", _APOSTROPHE.sub("", (name or "").upper()))
    words = [_EXPANSIONS.get(word, word) for word in _SPACES.split(folded) if word]
    return " ".join(words)


@dataclass(slots=True)
class GeocodeReport:
    total: int = 0
    matched: int = 0
    unmatched: int = 0
    unmatched_samples: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return (self.matched / self.total * 100) if self.total else 0.0

    def as_lines(self) -> list[str]:
        lines = [
            f"stations                  {self.total:>6}",
            f"  geocoded                {self.matched:>6}  ({self.coverage:.2f}%)",
            f"  unmatched               {self.unmatched:>6}",
        ]
        if self.unmatched_samples:
            lines.append("  examples: " + "; ".join(self.unmatched_samples[:8]))
        return lines


class CityIndex:
    """(city, state) -> (latitude, longitude), built from the GeoNames US dump.

    Where a state holds several places of the same name, the most populous one
    wins: truckstops sit beside the recognisable town, not its namesake hamlet.
    """

    def __init__(self) -> None:
        self._primary: dict[tuple[str, str], tuple[float, float, int]] = {}
        self._aliases: dict[tuple[str, str], tuple[float, float, int]] = {}
        self._squashed: dict[tuple[str, str], tuple[float, float, int]] = {}

    @classmethod
    def from_dump(cls, path: Path) -> "CityIndex":
        index = cls()
        with path.open(encoding="utf-8", newline="") as handle:
            for parts in csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
                if len(parts) <= COL_POPULATION:
                    continue
                if parts[COL_FEATURE_CLASS] != POPULATED_CLASS:
                    continue
                if parts[COL_FEATURE_CODE] in HISTORICAL_CODES:
                    continue

                state = parts[COL_ADMIN1].strip().upper()
                if len(state) != 2:
                    continue

                try:
                    lat = float(parts[COL_LAT])
                    lon = float(parts[COL_LON])
                    population = int(parts[COL_POPULATION] or 0)
                except ValueError:
                    continue

                for raw in (parts[COL_NAME], parts[COL_ASCII]):
                    index._offer(index._primary, raw, state, lat, lon, population)
                    index._offer(
                        index._squashed, raw, state, lat, lon, population, squash=True
                    )

                for raw in parts[COL_ALTERNATES].split(","):
                    index._offer(index._aliases, raw, state, lat, lon, population)

        return index

    @staticmethod
    def _offer(
        target: dict[tuple[str, str], tuple[float, float, int]],
        raw_name: str,
        state: str,
        lat: float,
        lon: float,
        population: int,
        squash: bool = False,
    ) -> None:
        key = normalize_city(raw_name)
        if squash:
            key = key.replace(" ", "")
        if not key:
            return
        existing = target.get((key, state))
        if existing is None or population > existing[2]:
            target[(key, state)] = (lat, lon, population)

    def lookup(self, city: str, state: str) -> tuple[float, float] | None:
        """Resolve a city, falling back to alternate names then to a prefix match."""
        key = (normalize_city(city), (state or "").upper())
        if not key[0] or not key[1]:
            return None

        for table in (self._primary, self._aliases):
            hit = table.get(key)
            if hit is not None:
                return hit[0], hit[1]

        # The two sources disagree on internal spacing for a handful of names
        # ("Mc Calla" against "McCalla", "Brookpark" against "Brook Park"), so
        # compare them with spacing removed before giving up.
        squashed = self._squashed.get((key[0].replace(" ", ""), key[1]))
        if squashed is not None:
            return squashed[0], squashed[1]

        return self._prefix_match(key)

    def _prefix_match(self, key: tuple[str, str]) -> tuple[float, float] | None:
        """Last resort for names carrying a suffix GeoNames omits.

        "BORDENTOWN TOWNSHIP" should still find "BORDENTOWN". Only the most
        populous candidate in the same state is considered, and single-word
        prefixes shorter than four characters are ignored as too ambiguous.
        """
        name, state = key
        head = name.split(" ")[0]
        if len(head) < 4:
            return None

        best: tuple[float, float, int] | None = None
        for (candidate, candidate_state), value in self._primary.items():
            if candidate_state != state:
                continue
            if candidate == head or name.startswith(candidate + " "):
                if best is None or value[2] > best[2]:
                    best = value

        return (best[0], best[1]) if best else None

    def __len__(self) -> int:
        return len(self._primary)
