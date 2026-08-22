# Loom script

Five minutes, hard ceiling. Every number below came off a real run, so quote
them as they are.

Author: Muhammad Shehzam

---

## Before you hit record

Run these in order. The last one matters most: the first call in the video has
to be a genuine cache miss.

```bash
cd spotter-fuel-route-api
source /Users/shehzam/Downloads/.venv/bin/activate

pytest -m live                      # confirms the public OSRM server is up
python manage.py load_stations      # confirms 6,626 stations are loaded
python manage.py shell -c "from django.core.cache import cache; cache.clear()"
python manage.py runserver
```

**Screen setup**

- Editor font at 16pt or larger. A reviewer may watch this on a laptop.
- Open these four files as tabs, left to right, in the order you will speak
  about them: `apps/stations/cleaning.py`, `apps/routing/optimizer.py`,
  `apps/routing/corridor.py`, `tests/test_p5_api.py`.
- Postman: create four requests up front, named as below. Do not build them on
  camera.
- Close Slack, mail and notifications. Hide the bookmarks bar.
- One browser tab on `http://127.0.0.1:8000/api/docs/`, one blank for the map.

**Do one full dry run.** Beat 5 is the one that overruns.

---

## 0:00 to 0:20 · What it is

**On screen:** README, top of the file.

> I'm Muhammad Shehzam. This is an API that takes a start and a finish in the
> USA and returns the route, the cheapest way to fuel a truck along it, and
> what that fuel costs. Five hundred mile range, ten miles per gallon, and one
> call to the routing API per journey.

Do not read the requirements table aloud. Scroll past it.

---

## 0:20 to 1:20 · It works

**On screen:** Postman, request **1 · Dallas to Chicago (cold)**. Hit Send.

While the response lands, say what it is doing. Then point at the numbers in
this order:

| Field | Value | Say |
|---|---|---|
| `route.total_distance_miles` | 966.29 | |
| `fuel.total_gallons` | 96.629 | "Distance over ten, exactly, which is what the brief asks for" |
| `fuel.total_cost_usd` | 275.97 | |
| `fuel.stops_count` | 6 | |
| `fuel.savings_usd` | 42.52 | "13% under what you'd pay fuelling at the corridor's average price" |
| `meta.external_api_calls` | **1** | "This is the constraint they set. One call" |

> Six stops, and look at the sizes: fifty gallons at Caddo Mills where it's
> $2.80, then one and a quarter gallons at Texarkana where it's dearer. It buys
> deep when fuel is cheap and takes only what it needs when it isn't.

---

## 1:20 to 1:50 · The map

**On screen:** copy `meta.map_url` into the browser.

> Dallas out to Texarkana, across to Memphis, then north to Chicago. Numbered
> pin at every stop.

Click stop 2 in the sidebar. The map pans and the popup opens.

> Name, price, gallons, cost, and how far off the road it sits. Three miles
> here.

---

## 1:50 to 2:20 · It is fast, and it stops calling out

**On screen:** Postman, request **2 · Dallas to Chicago (cached)**. Send.

> Same journey. Two point six milliseconds, and zero external calls.

**On screen:** request **3 · Same journey, worded differently**. Send.

> That one asked for "dallas" and "Chicago, Illinois". Different words, same
> cached answer, because the cache keys on resolved coordinates rather than on
> what someone typed.

**On screen:** request **4 · Los Angeles to New York**. Send.

> Two thousand seven hundred and ninety three miles. Eighteen stops, and no leg
> longer than the tank.

---

## 2:20 to 2:55 · The data problem

**On screen:** `data/fuel-prices-for-be-assessment.csv`, first few rows. Then
`apps/stations/cleaning.py`.

> The price file has no coordinates. There's a city and a state, and an address
> column that holds highway exits rather than street addresses, so ordinary
> geocoders do badly with it.

> Three things had to be decided before any of it was usable. Nine Canadian
> provinces are in the file and the brief says USA, so six hundred and twenty
> rows go. Four hundred and eighty seven stations repeat under one ID with
> different prices and no date column, so those are readings over time and I
> store the mean. Taking the minimum would quote a total the route can't
> actually achieve.

> Coordinates come from the GeoNames gazetteer, joined on city and state, at
> build time. 99.95% of stations resolve. Because it happens at build time and
> the result is committed, serving a request does no geocoding at all, and
> that's what keeps the call count at one.

---

## 2:55 to 3:45 · The algorithm

**On screen:** `apps/routing/optimizer.py`, the module docstring.

> This is the minimum cost refuelling problem, and a greedy rule solves it
> exactly.

Read the rule off the screen:

> At each stop, look as far ahead as the tank allows. If somewhere cheaper is
> reachable, buy just enough to get there. Otherwise fill up and drive to the
> cheapest stop in range.

> Fuel is only worth carrying past a station that costs more. So when something
> cheaper is in reach there's nothing to gain by buying past it, and when
> there isn't, every gallon the tank holds is cheaper here than anywhere you
> can get to.

Scroll to `_next_cheaper_index` and `_RangeMinimum`.

> Monotonic stack for the nearest cheaper stop ahead, sparse table for the
> cheapest within range. Each stop is visited once.

**On screen:** `tests/test_p4_optimizer.py`, `cheapest_by_linear_program`.

> And I don't just claim it's optimal. The suite states the same problem as a
> linear program and solves it with scipy, then checks the greedy against it on
> eighty five random layouts. That caught two bugs. One was the greedy checking
> whether it could finish before checking whether anything cheaper was
> reachable. The other was my first verification oracle, which was wrong itself.

---

## 3:45 to 4:10 · Making it quick

**On screen:** `apps/routing/corridor.py`, the module docstring.

> Los Angeles to New York comes back as thirty three thousand shape points, and
> there are six and a half thousand truckstops. Comparing everything to
> everything is two hundred million distance calculations.

> Shape points exist to draw a line, not to measure distance, so the route
> thins to one point every two miles. Then stations and route points go into
> half degree cells and only neighbours get compared. Thirteen to thirty two
> milliseconds.

---

## 4:10 to 4:40 · The tests

**On screen:** terminal.

```bash
pytest
```

> Two hundred and fifty seven tests, no network, sixteen seconds. Ninety four
> percent coverage.

While it runs, switch to `tests/test_p5_api.py` and highlight
`test_a_successful_route_makes_exactly_one_external_call`.

> The one call requirement isn't a line in a README. It's asserted.

---

## 4:40 to 5:00 · Close

**On screen:** README, then the repo root.

> Postgres and Redis under docker compose, because the job description names
> them. It also runs on SQLite with no infrastructure, so you can clone it and
> have it working in three commands.

> Swagger at slash api slash docs, and the Postman collection is in the repo
> with the assertions you just saw. Thanks for watching.

---

## Where you will overrun

Beat 5, the algorithm, at 50 seconds. Rehearse that one alone. If you are
behind at 3:45, cut the two bug stories to just: "the linear program caught two
bugs I wouldn't have found by reading the code."

Do not, on camera:

- Explain the greedy proof beyond the two sentences above.
- Read the requirements table.
- Scroll through `services.py` or `views.py`. They are plumbing.
- Apologise for anything.

## If OSRM is down while recording

`pytest -m live` before recording is what catches this. If it fails, set
`ROUTING_PROVIDER=valhalla` in `.env` and restart. The demo runs unchanged.
