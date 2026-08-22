"""Turn the supplied price file into one clean record per truckstop.

The raw file needs three decisions made explicitly, all of which are visible in
the returned :class:`CleaningReport` so the loader can print what it did:

1. It contains Canadian provinces. The brief scopes the problem to the USA, so
   those rows are dropped rather than geocoded into the wrong country.
2. It repeats stations under one OPIS ID with differing prices and no date
   column. Those are read as observations over time and averaged. Taking the
   minimum instead would make the optimizer report a cost it cannot achieve.
3. The same OPIS ID appears under slightly different trade names
   ("PILOT TRAVEL CENTER #1243" / "PILOT #1243"). The longest name wins, since
   it is the more complete one.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from statistics import fmean

# Provinces and territories that appear in the file's State column.
CANADIAN_REGIONS = frozenset(
    {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
)


@dataclass(slots=True)
class StationRecord:
    """One deduplicated truckstop, before geocoding."""

    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    retail_price: Decimal
    price_sample_count: int
    price_min: Decimal
    price_max: Decimal


@dataclass(slots=True)
class CleaningReport:
    raw_rows: int = 0
    dropped_non_us: int = 0
    dropped_unparseable: int = 0
    stations: int = 0
    stations_with_multiple_prices: int = 0
    widest_price_spread: Decimal = Decimal("0")
    dropped_states: set[str] = field(default_factory=set)

    def as_lines(self) -> list[str]:
        return [
            f"raw rows                  {self.raw_rows:>6}",
            f"dropped, outside the USA  {self.dropped_non_us:>6}"
            f"  ({', '.join(sorted(self.dropped_states)) or 'none'})",
            f"dropped, unparseable      {self.dropped_unparseable:>6}",
            f"unique stations           {self.stations:>6}",
            f"  with repeat price rows  {self.stations_with_multiple_prices:>6}",
            f"  widest price spread     ${self.widest_price_spread:>5}",
        ]


def _quantize(value: float | Decimal) -> Decimal:
    """Round to the model's 5 decimal places."""
    return Decimal(str(value)).quantize(Decimal("0.00001"))


def clean(csv_path: Path) -> tuple[list[StationRecord], CleaningReport]:
    """Read the price file and collapse it to one record per OPIS ID."""
    report = CleaningReport()
    grouped: dict[int, list[dict[str, str]]] = {}

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            report.raw_rows += 1

            state = (row.get("State") or "").strip().upper()
            if state in CANADIAN_REGIONS:
                report.dropped_non_us += 1
                report.dropped_states.add(state)
                continue

            try:
                opis_id = int(row["OPIS Truckstop ID"])
                float(row["Retail Price"])
            except (KeyError, TypeError, ValueError):
                report.dropped_unparseable += 1
                continue

            grouped.setdefault(opis_id, []).append(row)

    records = [_collapse(opis_id, rows, report) for opis_id, rows in grouped.items()]
    records.sort(key=lambda record: record.opis_id)
    report.stations = len(records)
    return records, report


def _collapse(opis_id: int, rows: list[dict[str, str]], report: CleaningReport) -> StationRecord:
    prices = [float(row["Retail Price"]) for row in rows]

    if len(prices) > 1 and len(set(prices)) > 1:
        report.stations_with_multiple_prices += 1
        spread = _quantize(max(prices) - min(prices))
        report.widest_price_spread = max(report.widest_price_spread, spread)

    # The fullest trade name is the most useful one for POI matching later.
    best = max(rows, key=lambda row: len((row.get("Truckstop Name") or "").strip()))

    try:
        rack_id = int(best["Rack ID"])
    except (KeyError, TypeError, ValueError):
        rack_id = None

    return StationRecord(
        opis_id=opis_id,
        name=(best.get("Truckstop Name") or "").strip(),
        address=(best.get("Address") or "").strip(),
        city=(best.get("City") or "").strip(),
        state=(best.get("State") or "").strip().upper(),
        rack_id=rack_id,
        retail_price=_quantize(fmean(prices)),
        price_sample_count=len(prices),
        price_min=_quantize(min(prices)),
        price_max=_quantize(max(prices)),
    )
