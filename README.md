# Fuel Route API

Give it two places in the USA. It returns the driving route, the cheapest
sequence of diesel stops for a truck with a 500 mile range at 10 miles per
gallon, and what the fuel costs.

Built by **Muhammad Shehzam** for the Spotter Backend Django Engineer
assessment.

```
POST /api/v1/route/   {"start": "Dallas, TX", "finish": "Chicago, IL"}

966.3 miles · 96.63 gallons · $275.97 · 6 stops · 1 external API call · 13.4% under the corridor average
```

---

## Quick start

Python 3.12 or newer. No API keys, no database server, no Docker.

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py load_stations
python manage.py runserver
```

Then:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "Dallas, TX", "finish": "Chicago, IL"}'
```

Swagger UI sits at `/api/docs/`. The response carries a `meta.map_url`; open
it in a browser for the route drawn on a map with a numbered pin at every stop.

### With PostgreSQL and Redis

```bash
docker compose up --build
```

The job description names PostgreSQL, so compose runs it alongside Redis. The
service also runs without either, falling back to SQLite and an in-process
cache, which is why a fresh clone needs three commands and no infrastructure.

Both paths are verified against real servers, not mocks. The whole suite runs
green on PostgreSQL 16.2 with Redis 6.2.14: 6,626 rows land in PostgreSQL,
nine indexes are created, and a repeated journey comes back out of Redis in
0.4 ms with the payload unchanged.

---

## What the brief asked for

| Requirement | Where it lives |
|---|---|
| API taking a start and finish in the USA | `POST` or `GET /api/v1/route/` |
| Return a map of the route | GeoJSON and an encoded polyline in the response, plus a Leaflet page at `meta.map_url` |
| Optimal fuel stops, meaning cost effective | `apps/routing/optimizer.py` |
| 500 mile range, so several fuel-ups | Tank capacity is a hard constraint; a route that cannot be fuelled is refused with the stretch named |
| Total money spent at 10 mpg | `fuel.total_cost_usd`, over gallons equal to distance divided by 10 |
| Use the attached price file | 8,151 rows cleaned, geocoded and loaded |
| Find a free map API | OSRM, which needs no key |
| Latest stable Django | Django 6.1 |
| Return results quickly | 2.6 to 6.3 ms warm, 695 to 1573 ms cold |
| One call ideal, two or three acceptable | **One.** A test asserts it |

---

## The problem the CSV poses

The price file has no coordinates:

```
OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
```

Nothing there can be placed on a map. The Address column holds highway-exit
descriptors rather than street addresses, and street geocoders do poorly with
those. Three other things needed deciding before any of it could be used:

**Canada is in the file.** Nine provinces appear in the State column. The brief
scopes the work to the USA, so 620 rows go, leaving 6,626 stations from 8,151
rows.

**487 stations carry several prices.** Same OPIS ID, same rack ID, different
price, no date column. Spread averages 10 cents and reaches 90. Those read as
observations over time, so the loader stores their mean and keeps
`price_sample_count`, `price_min` and `price_max` visible. Taking the minimum
would quote a total the route cannot achieve.

**One station, several trade names.** "PILOT TRAVEL CENTER #1243" and "PILOT
#1243" share an ID. The longer name wins.

### Geocoding

Coordinates come from the GeoNames US gazetteer, joined on city and state.
That needs no API key, hits no rate limit, and produces identical output on
every machine that clones the repo. Two rounds of measurement took coverage
from 99.73% to **99.95%**, 6,623 of 6,626:

- Apostrophes were being blanked into spaces, so GeoNames' `O'Neill` folded to
  `O NEILL` and never met the file's `Oneill`. Now they are stripped.
- The two sources disagree on internal spacing. `Mc Calla` against `McCalla`,
  `Brookpark` against `Brook Park`. A space-squashed lookup table catches those.
- Historical place codes are excluded, so a ghost town cannot outrank a live
  city sharing its name.

Three stations stay unresolved: `Pueblo Of Acoma, NM`, `Crescent, PA` and
`Corinth, ME`. They load with `geocode_precision: unknown` and never become
candidates.

### What I tried next, and dropped

The plan called for a second pass sharpening those centroids into real
forecourt positions by matching stations against OpenStreetMap fuel points
through Overpass. I built it around store numbers, since "LOVES TRAVEL STOP
#766" agreeing with a `Love's Travel Stop #766` on brand, number and state is
hard to reach by accident.

Overpass says otherwise. Of 477 named fuel points in Oklahoma, **none** carry a
store number. The names are bare: `Love's`, `Pilot`, `7-Eleven`, with no `ref`
tag either. That leaves brand and proximity, and a town with two Pilots then
offers a coin flip. Guessing wrong moves a station further from the truth than
the centroid it replaced.

So the pass is gone rather than shipped at lower confidence. City centroids
land one to three miles out, the corridor is ten miles wide, and every stop
reports its own measured detour. Sharper coordinates would change no stop the
optimizer picks.

City-centroid error runs one to three miles. Against a 500 mile tank and a ten
mile corridor that changes no decision.

**Geocoding runs at build time and its output is committed.** `load_stations`
needs no network, and serving a request performs no geocoding at all. That is
what keeps the external call count at one.

---

## How a request is served

```
POST /api/v1/route/
  │
  ├─ 1. Resolve both endpoints      committed gazetteer      0 calls
  ├─ 2. Look in the cache           Redis or in-process      0 calls
  ├─ 3. Fetch the route             OSRM                     1 call   ◄── the only egress
  ├─ 4. Match the corridor          resident NumPy arrays    0 calls
  ├─ 5. Choose the stops            greedy over a sparse table
  └─ 6. Serialise and cache
```

### Matching the corridor

Los Angeles to New York arrives as 33,643 shape points, and 6,623 stations sit
in memory. Comparing everything against everything runs to 223 million
distance calculations.

Two reductions avoid that. Shape points exist to draw a line, not to measure
proximity, so the route thins to one point every two miles: 33,643 becomes
1,393. Stations and route points then go into half-degree cells, and
comparisons run cell by cell against a handful of local points. The corridor
filter uses an equirectangular approximation instead of haversine, agreeing to
under a tenth of a mile across ten and costing one cosine rather than several.

Thinning would leave the reported detour off by up to half the sample spacing,
so the few hundred stations that survive get re-measured against the original
geometry. Detour and mile marker come back exact.

That whole stage takes 13 to 32 milliseconds.

### Choosing the stops

This is the classic minimum-cost refuelling problem, and a greedy rule solves
it exactly:

> At each stop, look as far ahead as the tank allows. If somewhere cheaper is
> reachable, buy just enough fuel to get there. Otherwise fill up, and drive to
> the cheapest stop in range.

The argument is an exchange one. Fuel bought at the current price is only worth
carrying past a station that costs more, so when a cheaper station is in reach
there is nothing to gain by buying beyond what it takes to arrive. When none
is, every gallon the tank holds is cheaper here than anywhere reachable.
Filling is capped at the fuel still needed to finish, since fuel that never
burns is money left in the tank.

Each stop is visited once. "Nearest cheaper stop ahead" comes from a monotonic
stack in linear time, and "cheapest stop within range" from a sparse table
answering in constant time, so building a plan costs O(n log n) and the routing
call dwarfs it.

**Optimality is verified, not asserted.** The suite states the same problem as
a linear program and solves it with scipy. Purchases are the variables, cost is
linear in them, and every rule is a linear constraint. Eighty-five randomised
layouts agree with the greedy to within 1e-9. Two earlier versions failed that
check, which is the point of having it.

---

## API

### `POST /api/v1/route/`

```json
{"start": "Dallas, TX", "finish": "Chicago, IL", "max_detour_miles": 10}
```

`start` and `finish` accept a city and state (`"Dallas, TX"`), a state spelled
out (`"Dallas, Texas"`), a bare city (`"Dallas"`, resolving to the largest of
that name), a city and code without a comma (`"Tomah WI"`), or a coordinate
pair (`"32.7767,-96.7970"`). Anything outside the USA is refused.

`GET /api/v1/route/?start=Dallas,+TX&finish=Chicago,+IL` does the same thing.

Response, trimmed:

```json
{
  "request": { "start": {"query": "Dallas, TX", "resolved": "Dallas, TX", "source": "gazetteer"}, "…": "…" },
  "vehicle": { "max_range_miles": 500.0, "mpg": 10.0, "tank_gallons": 50.0 },
  "route": {
    "total_distance_miles": 966.29,
    "total_duration_hours": 17.09,
    "geometry": { "type": "LineString", "coordinates": [[-96.80657, 32.78313], "…"] },
    "polyline": "…",
    "shape_points": 9174,
    "simplified_points": 479
  },
  "fuel": {
    "feasible": true,
    "total_gallons": 96.629,
    "total_cost_usd": 275.97,
    "average_price_per_gallon": 2.8559,
    "stops_count": 6,
    "naive_cost_usd": 318.49,
    "savings_usd": 42.52,
    "savings_percent": 13.35
  },
  "fuel_stops": [
    {
      "sequence": 2,
      "name": "CADOO MILLS",
      "city": "Caddo Mills", "state": "TX",
      "price_per_gallon": 2.8007,
      "gallons": 50.0,
      "cost_usd": 140.03,
      "distance_from_start_miles": 42.1,
      "detour_miles": 3.18
    }
  ],
  "meta": {
    "external_api_calls": 1,
    "cached": false,
    "compute_ms": 1727.37,
    "map_url": "/api/v1/route/map/1696d95182c9496cba9595c33507bd4f/"
  }
}
```

`savings_usd` compares the plan against fuelling the same trip at the
corridor's average price, which is roughly what a driver choosing stops at
random pays.

### Other endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/route/map/<token>/` | The plan drawn on Leaflet |
| `GET /api/v1/stations/` | Browse prices. Filter by `state`, `search`, `ordering` |
| `GET /api/v1/health/` | Liveness, plus the database and cache backend in use |
| `GET /api/docs/` | Swagger UI |

### Errors

| Status | When |
|---|---|
| 400 | A place cannot be found, sits outside the USA, or the request is malformed |
| 404 | No road connects the two points |
| 503 | Both routing providers are unreachable |

An unfuellable route returns 200 with `fuel.feasible: false` and a reason
naming the stretch, since the route itself is a valid answer:

> No fuel is available between mile 210 and mile 1100: that stretch is 890
> miles, beyond the 500 mile range of a full tank.

---

## Performance

Measured against the live OSRM server.

| Journey | Cold | Warm | Calls |
|---|---|---|---|
| Dallas to Chicago, 966 mi | 1165 ms | 2.6 ms | 1, then 0 |
| Los Angeles to New York, 2,793 mi | 695 ms | 6.3 ms | 1, then 0 |
| Seattle to Miami, 3,302 mi | 1573 ms | 4.4 ms | 1, then 0 |

Cold time is mostly OSRM. Local work runs 23 to 40 ms.

A warm plan issues one database query, to name the chosen stops. A cached plan
issues none. Both are asserted in the suite.

Caching keys on resolved coordinates rather than typed text, so `"dallas"` and
`"Dallas, TX"` share an answer. Vehicle settings are in the key too, so a plan
built for a 500 mile tank is never served to a 300 mile one.

---

## Assumptions

1. The truck departs with an empty tank and pays for every gallon it burns, so
   gallons equal distance divided by 10. That is what the brief asks for.
2. Before setting off it fills at the cheapest truckstop within 30 miles of the
   origin, which is what a driver does. The opening leg is charged there.
3. Repeated price rows for one station are observations over time, so the
   loader averages them.
4. Canadian rows are dropped.
5. Prices are diesel retail per gallon, as supplied.
6. Detour distance is reported but not added to fuel burn. Stops sit at
   interstate exits, so the error stays under 1%.
7. The plan can include a stop buying a fifth of a gallon. That is cost-optimal
   under the model, and no dispatcher would make it. A minimum-purchase
   constraint would fix it at a small premium and is left out on purpose,
   because it breaks the optimality the suite verifies.

Every constant lives in `config/settings.py` and reads from the environment.
Range, mileage, corridor width and origin radius all move without a code change.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest                      # 257 tests, no network, about 16 seconds
pytest -m live              # hits the real OSRM server
pytest --cov                # 94%
```

Worth knowing about:

- `test_planning_a_journey_makes_exactly_one_external_call` asserts the brief's
  headline constraint rather than claiming it in prose.
- `test_greedy_matches_the_linear_program_on_random_instances` checks
  optimality against an independent formulation.
- `test_loaded_coordinates_are_inside_the_united_states` catches a sign error
  or a swapped latitude and longitude pair.
- `test_a_payload_survives_a_pickle_round_trip` catches anything that would
  pass against the local cache and break against Redis.

The suite is run both ways: on SQLite with an in-process cache, and on
PostgreSQL with Redis. Backend selection is tested by reloading the settings
with each environment variable present and absent, so no test depends on what
happens to be running on the machine.

Run `pytest -m live` before any demo, to confirm the public routing server is up.

---

## Regenerating the data

`data/stations_geocoded.csv` and `data/us_places.csv` are committed, so nothing
below is needed to run the service. To rebuild them:

```bash
curl -L -o data/geonames_raw/US.zip https://download.geonames.org/export/dump/US.zip
unzip -o data/geonames_raw/US.zip -d data/geonames_raw

python manage.py build_station_data    # clean, geocode, write stations_geocoded.csv
python manage.py build_places_index    # write us_places.csv
python manage.py load_stations         # into the database
```

The dump runs to 307 MB unpacked and stays out of the repo.

---

## Layout

```
config/                     settings, urls, wsgi and asgi
apps/stations/
  models.py                 Station, with the price spread kept visible
  cleaning.py               drops Canada, averages repeats, picks the fullest name
  geocoding.py              GeoNames index and name folding
  management/commands/      build_station_data · build_places_index · load_stations
apps/routing/
  providers.py              OSRM, with Valhalla standing by
  polyline.py               encoded polyline codec
  geo.py                    vectorized haversine and its planar approximation
  corridor.py               thinning, grid bucketing, exact re-measurement
  optimizer.py              minimum-cost refuelling
  resolver.py               offline endpoint resolution
  services.py               orchestration and caching
  views.py                  the HTTP surface
data/                       the supplied price file, plus what is built from it
tests/                      one file per stage, plus the live checks
```

---

## Choices worth defending

**OSRM over anything with a key.** A reviewer clones the repo and it works. No
signup, no secret to share over email.

**A standby provider.** Public demo servers go down. Valhalla is consulted only
after OSRM has failed, never to improve an answer, so the healthy path stays at
one call. When it does fire, the response reports two calls rather than
understating what left the process.

**SQLite and LocMem fallbacks.** The job description names PostgreSQL and
Redis, and compose runs both. Requiring them to try the service would put a
Docker install between a reviewer and a working API.

**Averaging repeated prices instead of taking the minimum.** The minimum makes
the optimizer look better and the number would be wrong.

**A linear program in the test suite.** Verifying a greedy with another greedy
proves nothing. Two of my own versions passed a weaker check and were wrong.

---

Muhammad Shehzam · m.shehzamtariq@gmail.com
