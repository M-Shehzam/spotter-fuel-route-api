# Spotter Assessment — Build Plan

**Deliverable:** GitHub repo + 5-minute Loom, pasted into the Teamtailor question in Ena's email.
**Received:** Aug 21, 00:39. **3-day window → due ~Aug 24.** Today is Aug 22.

---

## 1. What they actually asked for

| # | Requirement | How we satisfy it |
|---|---|---|
| 1 | API takes start + finish, both in USA | `POST /api/v1/route/` with place names or `lat,lon` |
| 2 | Return a map of the route | GeoJSON LineString + encoded polyline in the response, plus a live Leaflet page at `/api/v1/route/map/<token>/` |
| 3 | Optimal (cheapest) fuel stops along the route | Min-cost refueling optimizer over corridor-matched truckstops |
| 4 | 500-mile max range, multiple fuel-ups | Tank capacity = 50 gal = 500 mi, enforced as a hard constraint |
| 5 | Total money spent on fuel at 10 mpg | `total_gallons = distance / 10`, cost = Σ (gallons bought × that stop's price) |
| 6 | Use the attached price file | 8,151 rows loaded, cleaned, geocoded, stored in Postgres |
| 7 | Find a free map/routing API | OSRM demo server — free, **no API key**, verified 200 OK at 518 ms |
| 8 | Latest stable Django | Django **6.1** (verified current on PyPI) + DRF 3.18 |
| 9 | Return results quickly | Warm path < 15 ms via cache; cold path bounded by the single OSRM call |
| 10 | Ideally 1 external call, 2–3 acceptable | **Exactly 1.** Endpoint geocoding is resolved offline, so only the route call leaves the process. Enforced by a test. |
| 11 | 5-min Loom, Postman + code walkthrough | Script in §9, Postman collection committed |
| 12 | Share GitHub code | Public repo `spotter-fuel-route-api` |

## 2. The hidden difficulty

The CSV has **no coordinates**. Nothing in it can be placed on a map as-is:

```
OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
```

Facts established from the file:

- 8,151 rows → **6,738 unique** truckstops (by OPIS ID)
- 4,275 unique city+state pairs
- 57 "states" — 9 are **Canadian provinces** (AB, BC, MB, NB, NS, ON, QC, SK, YT). The brief says USA, so 620 rows get dropped → **7,531 US rows / 6,626 US stations**
- 597 stations carry **multiple price observations** (same station, same rack ID, no date column). Spread averages $0.10, peaks at $0.90 → these are readings over time. We take the **mean** as the expected price and record `price_sample_count`. Using the min would make the optimizer look cheaper than reality.
- Addresses are highway-exit descriptors (`"I-44, EXIT 283 & US-69"`), not street addresses. Standard geocoders handle these badly — which drives the geocoding design below.

## 3. Geocoding — hybrid, resolved at build time

You picked hybrid. I'm moving the refinement pass to **build time** rather than request time, because refining during a request would add external calls and break requirement #10.

**Stage 1 — baseline (fast, total coverage).** Download the free GeoNames US places dataset once, build a `(city, state) → lat/lon` index, join it to the CSV. Runs in seconds, no API key, no rate limit, 100% coverage, and a reviewer who clones the repo gets identical output. Precision: city centroid, ~1–3 mi error — negligible against a 500-mile tank.

**Stage 2 — refinement (accuracy, additive).** A separate management command upgrades coordinates to true POI level by querying the **Overpass API** for `amenity=fuel` / `highway services` nodes per state bounding box, then fuzzy-matching on brand name + city. That's ~50 bulk queries instead of 6,738 individual geocodes, so it respects usage policy and finishes in minutes. Each station records `geocode_precision` = `city` | `poi`.

**Stage 3 — request time.** Zero geocoding calls. Everything is pre-resolved in the database and the result is committed to the repo as a CSV.

Stage 2 is deliberately **additive and droppable**. Stage 1 alone gives a fully working app, so if Overpass misbehaves we ship without it and lose nothing but precision.

## 4. Architecture

```
Client
  │  POST /api/v1/route/ {"start": "Dallas, TX", "finish": "Chicago, IL"}
  ▼
┌─────────────────────────────────────────────────────────────┐
│ DRF view                                                    │
│  1. Resolve endpoints    ← offline GeoNames index  (0 calls)│
│  2. Cache lookup         ← Redis / LocMem          (0 calls)│
│  3. Route                → OSRM                    (1 call) │◄── the only egress
│  4. Corridor match       ← in-memory NumPy         (0 calls)│
│  5. Optimize stops       ← greedy + monotonic deque(0 calls)│
│  6. Serialize + cache                                       │
└─────────────────────────────────────────────────────────────┘
  │
  ▼  JSON: route geometry · fuel stops · totals · map_url
```

**Stack:** Django 6.1 · DRF 3.18 · PostgreSQL (SQLite auto-fallback) · Redis (LocMem fallback) · NumPy · httpx · drf-spectacular · pytest-django · Docker Compose.

### Step 4 — corridor matching

A coast-to-coast polyline has thousands of points; testing all 6,626 stations against all of them is wasteful. Instead:

1. Downsample the polyline to one point every ~2 miles.
2. Bucket route points into 0.5° grid cells; collect only stations in touched cells plus their 1-ring neighbours.
3. Vectorized haversine over that reduced set → keep stations within `max_detour_miles` (default 10).
4. Tag each survivor with its `distance_along_route` from the nearest polyline index.

Linear in route length, sub-10 ms in NumPy.

### Step 5 — the optimizer

This is the algorithmic centrepiece and the part the job description is really testing ("scoring, ranking, routing, decision rules").

**Model.** Tank = 50 gal = 500 mi of range. The truck departs with an empty tank and pays for every gallon it burns, so `total_gallons = total_distance / 10` exactly — which is precisely what requirement #5 asks for. The origin is included as a purchase node priced at the cheapest candidate within 30 miles, matching how trucking actually works: you tank up before you roll. Constraints: first stop ≤ 500 mi in, every gap ≤ 500 mi, destination ≤ 500 mi past the last stop.

**Algorithm.** Textbook min-cost refueling, solved greedily:

> At each station, look ahead within range. If a cheaper station is reachable, buy *just enough* to reach the nearest cheaper one. Otherwise fill the tank completely and drive to the cheapest station in range.

Provably optimal, `O(n)` with a monotonic deque. Infeasible routes (a >500 mi gap with no truckstop) return `feasible: false` with the offending gap named, rather than a stack trace.

We also compute a **naive baseline** — the same trip fuelled at the corridor's average price — so the response can report actual dollars saved.

## 5. Repository layout

```
spotter-fuel-route-api/
├── config/                  settings (env-driven), urls, asgi/wsgi
├── apps/
│   ├── stations/            Station model, CSV loader, geocoders, admin
│   └── routing/             OSRM provider, corridor matcher, optimizer,
│                            serializers, views, Leaflet map template
├── data/
│   ├── fuel-prices-for-be-assessment.csv
│   └── stations_geocoded.csv       committed → no network needed to load
├── tests/                   optimizer, corridor, API, "exactly 1 call"
├── docker-compose.yml · Dockerfile · .env.example
├── postman_collection.json
├── requirements.txt
└── README.md
```

## 6. API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/route/` | Main endpoint |
| `GET` | `/api/v1/route/?start=&finish=` | Same, browser-friendly |
| `GET` | `/api/v1/route/map/<token>/` | Interactive Leaflet map |
| `GET` | `/api/v1/stations/` | Browse price data (filter, paginate) |
| `GET` | `/api/v1/health/` | Liveness + station count |
| `GET` | `/api/docs/` | Swagger UI |

Response carries `route` (geometry, distance, duration), `fuel` (gallons, cost, average price, savings vs naive), `fuel_stops[]` (name, address, price, gallons, cost, mile marker, detour), and `meta` (`external_api_calls`, `cached`, `compute_ms`, `map_url`).

## 7. Performance targets

| Path | Target |
|---|---|
| Cold (cache miss) | 400–800 ms, dominated by the OSRM call |
| Warm (cache hit) | **< 15 ms**, 0 external calls |
| Corridor + optimize | < 20 ms for a 2,800 mi route |

Stations load once into a module-level NumPy array (~6,600 × 3 floats) and stay resident. Route results are cached on a hash of the rounded endpoint coordinates.

## 8. Phases

Each phase ends in a working, committed state.

| Phase | Work | Est. |
|---|---|---|
| **P0** | Scaffold: venv, Django 6.1, DRF, settings, health endpoint, git init | 30 m |
| **P1** | Data pipeline: clean CSV, filter Canada, dedupe by mean price, GeoNames geocode, load command | 1.5 h |
| **P2** | OSRM provider: httpx client, polyline decode, timeout/retry, error mapping | 45 m |
| **P3** | Corridor matcher: grid index, vectorized haversine, distance-along-route | 1 h |
| **P4** | Optimizer: greedy + deque, feasibility, naive baseline, unit tests vs brute force | 1.5 h |
| **P5** | API layer: serializers, views, validation, Leaflet map page | 1.5 h |
| **P6** | Cache + perf: Redis/LocMem, warm-start, timing instrumentation | 45 m |
| **P7** | Tests: optimizer, corridor, API with mocked OSRM, **assert exactly 1 call** | 1 h |
| **P8** | Docker Compose, README (stop-slop pass), Postman collection, `.env.example` | 1 h |
| **P9** | Overpass refinement pass (droppable) | 45 m |
| **P10** | GitHub push, Loom rehearsal + record | 1 h |

Roughly 11 hours of build. Comfortable inside the remaining window.

## 9. Loom script — 5 minutes

Timings are targets; the cue column says exactly what should be on screen.

| Time | On screen | Say |
|---|---|---|
| 0:00–0:25 | README top | Who you are, what the API does in one sentence: start and finish in, cheapest fuel plan out. Name the constraints: 500 mi range, 10 mpg, one external call. |
| 0:25–1:10 | Postman, `POST /api/v1/route/` Dallas → Chicago. **Send.** | Point at the response as it lands: total distance, total gallons, total cost, the stop list. Call out `meta.external_api_calls: 1` and `compute_ms`. |
| 1:10–1:40 | Browser, the `map_url` from that response | The route line, numbered pins at each fuel stop. Hover one — name, price, gallons, cost. This is requirement #2, visually. |
| 1:40–2:10 | Postman, **Send the same request again** | Second call is a cache hit: sub-15 ms, zero external calls. Then a long run — LA → New York — to show multiple fuel-ups over 2,800 miles. |
| 2:10–2:50 | `apps/stations/` — the loader | The CSV ships with no coordinates. Explain the two-stage geocode: offline GeoNames baseline, Overpass POI refinement, both at build time so requests stay at one call. Mention dropping Canadian rows and averaging repeat price observations. |
| 2:50–3:40 | `apps/routing/optimizer.py` | The centrepiece. State the model in one line — pays for every gallon, 50-gallon tank. Then the greedy rule: if a cheaper station is reachable, buy just enough to get there; otherwise fill up. Say it's provably optimal and `O(n)` with the deque. |
| 3:40–4:10 | `apps/routing/corridor.py` | Why you don't test 6,600 stations against thousands of polyline points: grid bucketing plus vectorized haversine, under 10 ms. |
| 4:10–4:40 | Terminal, `pytest -q` | Tests green. Highlight the one asserting the endpoint makes **exactly one** external call — the requirement, enforced in CI rather than promised. |
| 4:40–5:00 | README, repo root | Postgres + Docker Compose, Swagger docs, Postman collection. Repo link. Thanks. |

**Pre-record checklist**
- Server running, database loaded, cache **cleared** (so beat 2 is a genuine cold call)
- Postman tabs pre-created: Dallas→Chicago, LA→New York, and the repeat
- Editor font bumped to ~16pt, files already open in tabs in script order
- Close Slack, email, notifications; hide bookmarks bar
- Do one full dry run before the take — 5 minutes is a hard ceiling and beat 6 is the one that runs long

## 10. Open assumptions (stated in the README)

1. Truck departs empty and pays for every gallon burned → `total_gallons = distance / 10`.
2. Origin counts as a fuel stop, priced at the cheapest truckstop within 30 miles.
3. Repeat price rows for one station are observations over time → averaged.
4. Canadian rows dropped; the brief specifies USA.
5. Prices are treated as diesel retail per gallon, as supplied.
6. Detour distance to a stop is reported but not added to fuel burn — stops sit on interstate exits, so the error is under 1%.
