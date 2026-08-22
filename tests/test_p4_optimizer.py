"""P4 gate: the fuelling plan is genuinely the cheapest one available.

The greedy rule is checked against an exhaustive search. For a small number of
stops every subset can be priced directly, so the two must agree on every
instance; if they ever diverge, the greedy is wrong.
"""

import math
import random

import numpy as np
from scipy.optimize import linprog

import pytest

from apps.routing.corridor import Candidate
from apps.routing.optimizer import plan_fuel_stops

TANK_RANGE = 500.0
MPG = 10.0
ORIGIN_RADIUS = 30.0


def stops_from(pairs: list[tuple[float, float]]) -> list[Candidate]:
    """Build candidates from (mile marker, price) pairs."""
    return [
        Candidate(
            opis_id=index + 1,
            price=price,
            latitude=40.0,
            longitude=-100.0 + mile / 50.0,
            distance_along_route_miles=mile,
            detour_miles=0.0,
        )
        for index, (mile, price) in enumerate(sorted(pairs))
    ]


def plan(pairs, total_miles, tank=TANK_RANGE, radius=ORIGIN_RADIUS):
    return plan_fuel_stops(
        stops_from(pairs),
        total_miles,
        tank_range_miles=tank,
        mpg=MPG,
        origin_radius_miles=radius,
    )


def cheapest_by_linear_program(pairs, total_miles, tank=TANK_RANGE, radius=ORIGIN_RADIUS):
    """The true optimum, found by linear programming.

    Let ``x_i`` be the miles of range bought at stop ``i``. Cost is linear in
    those purchases and every rule of the problem is a linear constraint, so
    the exact optimum falls out of an LP:

        minimise    sum of x_i * price_i / mpg
        subject to  running total through stop i covers the next stop
                    running total minus distance travelled never exceeds a tank
                    total bought equals the length of the trip
                    x_i >= 0, and zero before the departure stop

    This is the independent check the greedy is measured against. An earlier
    version of this oracle priced each leg at the stop that began it, which
    quietly forbids carrying cheap fuel past a dearer stop, and so reported
    optima the greedy could beat.
    """
    ordered = sorted(pairs)
    positions = [mile for mile, _ in ordered]
    prices = [price for _, price in ordered]
    count = len(ordered)

    nearby = [i for i in range(count) if positions[i] <= radius]
    start = min(nearby, key=lambda i: prices[i]) if nearby else 0

    if positions[start] > tank or total_miles - positions[-1] > tank:
        return math.inf
    if any(positions[b] - positions[a] > tank for a, b in zip(range(start, count - 1), range(start + 1, count))):
        return math.inf

    objective = [prices[i] / MPG for i in range(count)]
    upper_rows: list[list[float]] = []
    upper_bounds: list[float] = []

    def running_total_row(through: int) -> list[float]:
        return [1.0 if k <= through else 0.0 for k in range(count)]

    for i in range(start, count):
        running = running_total_row(i)
        next_position = positions[i + 1] if i + 1 < count else total_miles

        # Enough bought by stop i to reach whatever comes next.
        upper_rows.append([-value for value in running])
        upper_bounds.append(-next_position)

        # Never carrying more than a tank on departure.
        upper_rows.append(running)
        upper_bounds.append(tank + positions[i])

    # The opening leg is settled at the departure stop.
    upper_rows.append([-value for value in running_total_row(start)])
    upper_bounds.append(-positions[start])

    bounds = [(0.0, 0.0) if i < start else (0.0, None) for i in range(count)]

    solution = linprog(
        c=objective,
        A_ub=np.array(upper_rows, dtype=float),
        b_ub=np.array(upper_bounds, dtype=float),
        A_eq=np.ones((1, count)),
        b_eq=np.array([total_miles], dtype=float),
        bounds=bounds,
        method="highs",
    )

    return solution.fun if solution.success else math.inf


# --------------------------------------------------------------------------
# Optimality
# --------------------------------------------------------------------------


def test_greedy_matches_the_linear_program_on_a_worked_example():
    pairs = [(10, 3.60), (120, 3.10), (260, 3.90), (390, 3.20), (520, 2.95), (700, 3.70)]

    result = plan(pairs, 900.0)

    assert result.feasible
    assert result.total_cost == pytest.approx(cheapest_by_linear_program(pairs, 900.0))


@pytest.mark.parametrize("seed", range(60))
def test_greedy_matches_the_linear_program_on_random_instances(seed):
    """Sixty randomised layouts, each verified against the exact optimum."""
    rng = random.Random(seed)
    count = rng.randint(3, 11)
    total_miles = rng.uniform(400, 2200)

    # Spacing stays inside the tank range so instances are mostly feasible.
    miles = sorted(rng.uniform(0, total_miles) for _ in range(count))
    pairs = [(round(mile, 2), round(rng.uniform(2.70, 5.20), 3)) for mile in miles]

    result = plan(pairs, total_miles)
    expected = cheapest_by_linear_program(pairs, total_miles)

    if math.isinf(expected):
        assert not result.feasible
    else:
        assert result.feasible
        assert result.total_cost == pytest.approx(expected, rel=1e-9)


@pytest.mark.parametrize("seed", range(25))
def test_greedy_stays_optimal_with_a_small_tank(seed):
    """A tighter tank forces more stops and exercises the fill-up branch."""
    rng = random.Random(1000 + seed)
    total_miles = rng.uniform(300, 900)
    miles = sorted(rng.uniform(0, total_miles) for _ in range(rng.randint(6, 12)))
    pairs = [(round(mile, 2), round(rng.uniform(2.70, 5.20), 3)) for mile in miles]

    result = plan(pairs, total_miles, tank=150.0)
    expected = cheapest_by_linear_program(pairs, total_miles, tank=150.0)

    if math.isinf(expected):
        assert not result.feasible
    else:
        assert result.feasible
        assert result.total_cost == pytest.approx(expected, rel=1e-9)


# --------------------------------------------------------------------------
# The brief's arithmetic
# --------------------------------------------------------------------------


def test_every_gallon_burned_is_paid_for():
    """The brief asks for the money spent at 10 mpg, so the gallons bought must
    equal the distance divided by the mileage: no more, no less."""
    result = plan([(10, 3.20), (300, 3.00), (600, 3.40)], 900.0)

    assert result.total_gallons == pytest.approx(900.0 / MPG)


def test_no_fuel_is_bought_that_the_trip_will_not_burn():
    """Topping up beyond the finish line is money left in the tank."""
    result = plan([(5, 2.80), (200, 4.90), (400, 4.95)], 450.0)

    assert result.total_gallons == pytest.approx(45.0)
    assert result.total_cost == pytest.approx(sum(stop.cost for stop in result.stops))


def test_cost_is_the_sum_of_its_stops():
    result = plan([(20, 3.10), (300, 2.90), (700, 3.50)], 1000.0)

    assert result.total_cost == pytest.approx(sum(stop.cost for stop in result.stops))
    assert result.total_gallons == pytest.approx(sum(stop.gallons for stop in result.stops))


def test_average_price_sits_between_the_cheapest_and_dearest_stop():
    result = plan([(10, 3.00), (400, 4.00), (800, 3.50)], 1100.0)

    used = [stop.candidate.price for stop in result.stops]
    assert min(used) <= result.average_price_per_gallon <= max(used)


# --------------------------------------------------------------------------
# The greedy rule itself
# --------------------------------------------------------------------------


def test_only_enough_is_bought_to_reach_somewhere_cheaper():
    """Dear now, cheap in 200 miles: buy 200 miles of fuel, not a full tank."""
    result = plan([(0, 4.50), (200, 2.80)], 600.0)

    first = result.stops[0]
    assert first.candidate.price == 4.50
    assert first.gallons == pytest.approx(200.0 / MPG)


def test_the_tank_is_filled_when_nothing_cheaper_is_in_reach():
    """Cheap now, dearer everywhere ahead: take all the tank will hold."""
    result = plan([(0, 2.80), (300, 4.60), (700, 4.80)], 1000.0)

    first = result.stops[0]
    assert first.candidate.price == 2.80
    assert first.gallons == pytest.approx(TANK_RANGE / MPG)


def test_a_single_cheap_stop_serves_a_trip_inside_one_tank():
    result = plan([(10, 2.90), (150, 4.50), (300, 4.60)], 400.0)

    assert result.stop_count == 1
    assert result.stops[0].candidate.price == 2.90
    assert result.total_gallons == pytest.approx(40.0)


def test_stations_passed_without_buying_are_not_reported_as_stops():
    result = plan([(10, 2.80), (100, 4.90), (200, 4.95), (300, 4.99)], 450.0)

    assert [stop.candidate.price for stop in result.stops] == [2.80]


def test_stops_come_back_in_travelling_order():
    result = plan(
        [(10, 3.30), (400, 3.10), (900, 3.60), (1300, 2.95), (1700, 3.40)], 2000.0
    )

    markers = [stop.mile_marker for stop in result.stops]
    assert markers == sorted(markers)
    assert [stop.sequence for stop in result.stops] == list(range(1, len(markers) + 1))


def test_the_tank_is_never_overfilled():
    result = plan(
        [(0, 3.00), (200, 3.40), (450, 2.90), (800, 3.80), (1100, 3.20)], 1400.0
    )

    for stop in result.stops:
        assert stop.departure_range_miles <= TANK_RANGE + 1e-6
        assert stop.arrival_range_miles >= -1e-9


def test_the_truck_never_runs_dry_between_stops():
    result = plan(
        [(0, 3.00), (480, 3.40), (950, 2.90), (1400, 3.80)], 1800.0
    )

    markers = [stop.mile_marker for stop in result.stops]
    for previous, following in zip(markers, markers[1:]):
        assert following - previous <= TANK_RANGE + 1e-6
    assert 1800.0 - markers[-1] <= TANK_RANGE + 1e-6


# --------------------------------------------------------------------------
# Departure fill
# --------------------------------------------------------------------------


def test_departure_uses_the_cheapest_stop_near_the_origin():
    result = plan([(5, 3.90), (12, 3.10), (25, 3.40), (400, 3.80)], 700.0)

    assert result.stops[0].candidate.price == 3.10


def test_a_cheap_stop_beyond_the_radius_does_not_win_departure():
    result = plan([(10, 3.90), (200, 2.60), (600, 3.80)], 900.0)

    assert result.stops[0].mile_marker == 10


def test_departure_falls_back_to_the_first_stop_when_none_is_close():
    """Nothing within the radius, so the opening leg is charged where it can be."""
    result = plan([(300, 3.20), (700, 3.50)], 1000.0)

    assert result.stops[0].mile_marker == 300
    assert result.total_gallons == pytest.approx(100.0)


# --------------------------------------------------------------------------
# Infeasibility
# --------------------------------------------------------------------------


def test_a_gap_wider_than_the_tank_is_refused_not_fudged():
    result = plan([(10, 3.20), (900, 3.10)], 1200.0)

    assert not result.feasible
    assert result.largest_gap_miles == pytest.approx(890.0)
    assert "890 miles" in result.infeasible_reason
    assert result.stops == []


def test_an_unreachable_first_stop_names_the_opening_stretch():
    result = plan([(700, 3.20)], 900.0)

    assert not result.feasible
    assert "origin and the first truckstop" in result.infeasible_reason


def test_an_unreachable_finish_names_the_closing_stretch():
    result = plan([(100, 3.20), (300, 3.10)], 1000.0)

    assert not result.feasible
    assert "destination" in result.infeasible_reason


def test_a_route_with_no_truckstops_is_refused():
    result = plan([], 500.0)

    assert not result.feasible
    assert "corridor" in result.infeasible_reason


def test_a_zero_length_route_costs_nothing():
    result = plan([(0, 3.20)], 0.0)

    assert result.feasible
    assert result.total_cost == 0.0
    assert result.stops == []


# --------------------------------------------------------------------------
# Reported savings
# --------------------------------------------------------------------------


def test_savings_are_measured_against_the_corridor_average():
    pairs = [(10, 2.80), (300, 4.60), (600, 4.80), (900, 3.00)]

    result = plan(pairs, 1200.0)

    average = sum(price for _, price in pairs) / len(pairs)
    assert result.naive_cost == pytest.approx(result.total_gallons * average)
    assert result.savings == pytest.approx(result.naive_cost - result.total_cost)
    assert result.savings > 0


def test_optimising_never_costs_more_than_the_average():
    for seed in range(20):
        rng = random.Random(seed)
        total_miles = rng.uniform(600, 1800)
        miles = sorted(rng.uniform(0, total_miles) for _ in range(8))
        pairs = [(round(m, 2), round(rng.uniform(2.7, 5.2), 3)) for m in miles]

        result = plan(pairs, total_miles)

        if result.feasible:
            assert result.total_cost <= result.naive_cost + 1e-9


def test_savings_vanish_when_every_stop_costs_the_same():
    result = plan([(10, 3.30), (300, 3.30), (700, 3.30)], 1000.0)

    assert result.savings == pytest.approx(0.0, abs=1e-9)
