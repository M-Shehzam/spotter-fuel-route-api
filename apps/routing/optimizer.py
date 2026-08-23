"""Choose where to buy fuel, and how much, for the least money.

The model
--------
The truck holds 500 miles of range (50 gallons at 10 mpg) and pays for every
gallon it burns, so the gallons bought over a trip equal its distance divided
by the mileage, exactly as the brief describes. Before departing it fills at
the cheapest truckstop within a short radius of the origin, which is what a
driver does; the opening leg is charged at that stop's
price. From there the plan must never ask the truck to cover more than a tank
between purchases, nor to finish more than a tank past its last one.

The algorithm
-------------
This is the classic minimum-cost refuelling problem, and a greedy rule solves
it exactly:

    At each stop, look as far ahead as the tank allows.
    If somewhere cheaper is reachable, buy just enough fuel to get there.
    Otherwise fill up, and drive to the cheapest stop in range.

The reasoning is an exchange argument. Fuel bought at the current price is only
worth carrying past a station that costs more, so when a cheaper station is
within reach there is no gain in buying beyond what it takes to arrive there,
and when there is not, every gallon the tank can hold is cheaper here than
anywhere reachable. Filling is additionally capped at the fuel still needed to
finish, since fuel that never gets burned is money wasted.

Each stop is visited at most once. "Nearest cheaper station ahead" comes from
a monotonic stack in linear time, and "cheapest station within range" from a
sparse table answering in constant time, so the whole plan costs O(n log n) to
build and is dominated by the routing call regardless.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from apps.routing.corridor import Candidate


@dataclass(slots=True)
class FuelStop:
    """One purchase in the plan."""

    sequence: int
    candidate: Candidate
    gallons: float
    cost: float
    arrival_range_miles: float
    departure_range_miles: float

    @property
    def mile_marker(self) -> float:
        return self.candidate.distance_along_route_miles


@dataclass(slots=True)
class FuelPlan:
    """The complete fuelling plan for one route."""

    feasible: bool
    stops: list[FuelStop] = field(default_factory=list)
    total_gallons: float = 0.0
    total_cost: float = 0.0
    average_price_per_gallon: float = 0.0
    naive_cost: float = 0.0
    savings: float = 0.0
    candidates_considered: int = 0
    largest_gap_miles: float = 0.0
    infeasible_reason: str | None = None

    @property
    def stop_count(self) -> int:
        return len(self.stops)


def plan_fuel_stops(
    candidates: list[Candidate],
    total_miles: float,
    *,
    tank_range_miles: float,
    mpg: float,
    origin_radius_miles: float,
) -> FuelPlan:
    """Build the cheapest feasible fuelling plan for a route."""
    if total_miles <= 0:
        return FuelPlan(feasible=True, candidates_considered=len(candidates))

    if not candidates:
        return FuelPlan(
            feasible=False,
            candidates_considered=0,
            infeasible_reason=(
                "No truckstops from the price file lie within the search corridor "
                "of this route."
            ),
        )

    positions = np.array([c.distance_along_route_miles for c in candidates], dtype=np.float64)
    prices = np.array([c.price for c in candidates], dtype=np.float64)
    count = positions.size

    # A stop is only usable if the truck can reach it from the one before, so
    # the widest gap between consecutive stops decides feasibility outright.
    bounds = np.concatenate(([0.0], positions, [total_miles]))
    gaps = np.diff(bounds)
    largest_gap = float(gaps.max())

    if largest_gap > tank_range_miles:
        return FuelPlan(
            feasible=False,
            candidates_considered=count,
            largest_gap_miles=largest_gap,
            infeasible_reason=_describe_gap(gaps, bounds, tank_range_miles, total_miles),
        )

    start = _departure_stop(positions, prices, origin_radius_miles)
    next_cheaper = _next_cheaper_index(prices)
    window = _RangeMinimum(prices)

    gallons_bought = np.zeros(count, dtype=np.float64)
    visits: list[tuple[int, float, float]] = []  # index, arrival range, departure range

    # The truck leaves with an empty tank and settles the opening leg at its
    # first stop, so every gallon burned on the route is paid for.
    opening_gallons = float(positions[start]) / mpg
    gallons_bought[start] += opening_gallons

    current = start
    fuel = 0.0  # miles of range in the tank on arrival

    while True:
        arrival = fuel
        remaining = total_miles - float(positions[current])
        reach = float(positions[current]) + tank_range_miles
        furthest = int(np.searchsorted(positions, reach, side="right")) - 1
        cheaper = int(next_cheaper[current])

        if cheaper != -1 and cheaper <= furthest:
            # Somewhere cheaper is in reach. Take it even when the destination
            # is also in reach: every stop lies before the finish, so filling
            # here at the higher price would only overpay for the same miles.
            leg = float(positions[cheaper]) - float(positions[current])
            shortfall = leg - fuel
            if shortfall > 0:
                gallons_bought[current] += shortfall / mpg
                fuel += shortfall
            visits.append((current, arrival, fuel))
            fuel -= leg
            current = cheaper
            continue

        if remaining <= tank_range_miles:
            # Nothing cheaper lies ahead within range, so finish from here and
            # buy only what the remaining miles will burn.
            shortfall = remaining - fuel
            if shortfall > 0:
                gallons_bought[current] += shortfall / mpg
                fuel += shortfall
            visits.append((current, arrival, fuel))
            break

        # Nothing cheaper is reachable and the finish is too far, so this is
        # the best price available for as far as the tank reaches. Fill it,
        # then move to the cheapest stop in range.
        fill = min(tank_range_miles - fuel, remaining - fuel)
        if fill > 0:
            gallons_bought[current] += fill / mpg
            fuel += fill
        target = int(window.argmin(current + 1, furthest))
        leg = float(positions[target]) - float(positions[current])

        visits.append((current, arrival, fuel))
        fuel -= leg
        current = target

    stops = _build_stops(candidates, gallons_bought, visits)
    total_gallons = float(gallons_bought.sum())
    total_cost = float((gallons_bought * prices).sum())

    # What the same trip would cost fuelling without regard to price: the
    # corridor's own average. It is the honest comparison, since a driver
    # picking stops at random pays about the mean.
    corridor_average = float(prices.mean())
    naive_cost = total_gallons * corridor_average

    return FuelPlan(
        feasible=True,
        stops=stops,
        total_gallons=total_gallons,
        total_cost=total_cost,
        average_price_per_gallon=(total_cost / total_gallons) if total_gallons else 0.0,
        naive_cost=naive_cost,
        savings=naive_cost - total_cost,
        candidates_considered=count,
        largest_gap_miles=largest_gap,
    )


def _departure_stop(positions: np.ndarray, prices: np.ndarray, radius_miles: float) -> int:
    """The stop the truck fills at before setting off.

    The cheapest truckstop within the radius wins. When the origin sits far
    from any of them the first stop on the route is used instead, and the
    opening leg is charged there.
    """
    nearby = np.flatnonzero(positions <= radius_miles)
    if nearby.size == 0:
        return 0
    return int(nearby[int(np.argmin(prices[nearby]))])


def _next_cheaper_index(prices: np.ndarray) -> np.ndarray:
    """For each stop, the nearest stop ahead that costs strictly less, or -1.

    A monotonic stack: every index is pushed and popped once, so this is linear
    in the number of stops.
    """
    count = prices.size
    result = np.full(count, -1, dtype=np.int64)
    stack: list[int] = []

    for index in range(count):
        price = prices[index]
        while stack and prices[stack[-1]] > price:
            result[stack.pop()] = index
        stack.append(index)

    return result


class _RangeMinimum:
    """Sparse table over prices: cheapest stop in any span, in constant time.

    Built once per route in O(n log n). The greedy asks this whenever no
    cheaper stop is reachable and it must pick the best of a bad set.
    """

    def __init__(self, prices: np.ndarray) -> None:
        self._prices = prices
        count = prices.size
        levels = max(1, count.bit_length())
        self._table: list[np.ndarray] = [np.arange(count, dtype=np.int64)]

        for level in range(1, levels):
            span = 1 << level
            if span > count:
                break
            previous = self._table[level - 1]
            left = previous[: count - span + 1]
            right = previous[span // 2 : count - span // 2 + 1]
            self._table.append(np.where(prices[left] <= prices[right], left, right))

    def argmin(self, low: int, high: int) -> int:
        """Index of the cheapest stop in the inclusive span ``[low, high]``."""
        # NumPy integers arrive here from searchsorted and the monotonic stack,
        # and they carry none of the Python integer methods used below.
        low = int(low)
        high = int(high)
        if high < low:
            raise ValueError(f"Empty span [{low}, {high}]")

        level = (high - low + 1).bit_length() - 1
        span = 1 << level
        left = int(self._table[level][low])
        right = int(self._table[level][high - span + 1])
        return left if self._prices[left] <= self._prices[right] else right


def _build_stops(
    candidates: list[Candidate],
    gallons_bought: np.ndarray,
    visits: list[tuple[int, float, float]],
) -> list[FuelStop]:
    """Turn purchases into an ordered plan, dropping stations passed without buying."""
    stops: list[FuelStop] = []
    sequence = 0

    for index, arrival, departure in visits:
        gallons = float(gallons_bought[index])
        if gallons <= 1e-9:
            continue
        sequence += 1
        candidate = candidates[index]
        stops.append(
            FuelStop(
                sequence=sequence,
                candidate=candidate,
                gallons=gallons,
                cost=gallons * candidate.price,
                arrival_range_miles=max(0.0, arrival),
                departure_range_miles=max(0.0, departure),
            )
        )

    return stops


def _describe_gap(
    gaps: np.ndarray, bounds: np.ndarray, tank_range_miles: float, total_miles: float
) -> str:
    """Name the stretch that cannot be crossed, rather than failing vaguely."""
    worst = int(np.argmax(gaps))
    start_mile = float(bounds[worst])
    end_mile = float(bounds[worst + 1])
    span = end_mile - start_mile

    if worst == 0:
        where = f"between the origin and the first truckstop at mile {end_mile:.0f}"
    elif worst == gaps.size - 1:
        where = f"between the last truckstop at mile {start_mile:.0f} and the destination"
    else:
        where = f"between mile {start_mile:.0f} and mile {end_mile:.0f}"

    return (
        f"No fuel is available {where}: that stretch is {span:.0f} miles, "
        f"beyond the {tank_range_miles:.0f} mile range of a full tank. "
        f"The route is {total_miles:.0f} miles long."
    )
