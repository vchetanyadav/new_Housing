# Housing Opportunity Finder

[![tests](https://github.com/vchetanyadav/new_Housing/actions/workflows/tests.yml/badge.svg)](https://github.com/vchetanyadav/new_Housing/actions/workflows/tests.yml)

Finds under-used residential lots within walking distance of Melbourne train stations, checks them against
live satellite imagery, and estimates how many homes a rezoning scenario could unlock and the extra demand
on water, power and schools. Pilot council: Merri-bek.

It shows **opportunities to investigate**. It does not decide whether land can or should be rezoned.

---

## How the system fits together

```
 Browser
    │  http://localhost:8080
    ▼
┌──────────────────────────────┐
│  web  (nginx gateway)        │  middleware: serves the frontend, proxies /api,
│  frontend/ + gateway/        │  rate limits, gzip, security headers, caching,
└──────────────┬───────────────┘  optional login
               │ /api/*
               ▼
┌──────────────────────────────┐
│  api  (FastAPI, Python)      │  backend: scoring, scenarios, lot evidence,
│  backend/api + backend/src   │  exports; reloads data automatically
└──────────────┬───────────────┘
               │ reads
               ▼
        [ housing-data volume ]   parquet tables + satellite rasters
               ▲ writes
┌──────────────┴───────────────┐
│  pipeline / scheduler        │  jobs: download Vicmap, GTFS, OSM, ABS,
│  (same backend image)        │  Sentinel-2; build the lot table
└──────────────────────────────┘
```

| Folder | Layer | What's in it |
|---|---|---|
| `frontend/` | Frontend | `index.html`, `app.js`, `styles.css`, bundled MapLibre map library |
| `gateway/` | Middleware | nginx config, security headers, login script, Dockerfile |
| `backend/api/` | Backend API | FastAPI app (`main.py`) and in-memory data store (`store.py`) |
| `backend/src/` | Data pipeline + logic | ingest, satellite, walking network, census, scoring, infrastructure, brief |
| `backend/tests/` | Tests | synthetic data generator, pipeline tests, API tests |
| `docker-compose.yml` | Orchestration | `web`, `api`, `pipeline` (job), `scheduler` (job) |

---

## Step-by-step: run the full stack on your computer

### Step 1 — Install Docker
- **Windows / Mac:** install Docker Desktop from docker.com and start it.
- **Linux:** install Docker Engine and the Compose plugin.
- Check: `docker compose version` should print **v2.24 or newer**.
- Give Docker at least **8 GB of memory** (Docker Desktop → Settings → Resources) and keep ~10 GB of disk free.

### Step 2 — Unpack the project and create your settings file
```bash
cd smart-housing
cp .env.example .env          # Windows PowerShell: copy .env.example .env
```
The defaults work as they are. `.env` is where you later set a login, port, or refresh interval.

### Step 3 — Build and start the website and API
```bash
docker compose up -d --build
```
The first build takes 5–10 minutes. Check both services are running:
```bash
docker compose ps             # api and web should show "healthy" / "running"
```
Open **http://localhost:8080**. You'll see "No data loaded" — that's expected; the next step loads data.

### Step 4 — Try it with demo data (2 minutes)
```bash
docker compose --profile jobs run --rm pipeline demo
```
Reload the browser. You should see a small synthetic neighbourhood, results in the dark panel, and lots on the map.
Click a lot to see its evidence. This confirms every layer works before you download real data.

### Step 5 — Check every data source (1 minute)
```bash
docker compose --profile jobs run --rm pipeline verify
```
This tests each government and open-data source: that it responds, that the Vicmap layers still have
the columns the pipeline needs, that the GTFS link works, and that recent satellite scenes exist.
Each line says PASS, FAIL or WARN with the reason. If anything required fails, paste the output into the
chat before going further — it's much quicker to fix from this than from a half-finished download.

### Step 6 — Load the real Merri-bek data (30–60 minutes)
```bash
docker compose --profile jobs run --rm pipeline
```
Downloads Vicmap parcels, zones and overlays, train stations, building footprints, ABS Census data and the
latest Sentinel-2 passes, then builds the lot table. It removes the demo data first. Reload the page when it
finishes. Open **Data sources** at the bottom of the left panel to see when each dataset was retrieved and
any warnings (for example, if many lots have no building footprint).

If a download fails, the previous copy (if any) is kept, the failure is shown in the app, and you can
re-run the same command — completed downloads are skipped. To force every map layer to download again
from scratch: `docker compose --profile jobs run --rm pipeline fresh`.

### Step 6b — Keep everything current
```bash
docker compose --profile scheduler up -d scheduler
```
Once a day (`REFRESH_SECONDS`) the scheduler re-downloads every source older than `REFRESH_MAX_AGE_DAYS`
(default 7), refreshes the satellite imagery and rebuilds the lot table. A failed refresh never replaces
good data. Census figures stay at 2021 until the ABS releases 2026 results.
To refresh once by hand: `docker compose --profile jobs run --rm pipeline refresh`.

### Step 7 — Run the tests
```bash
docker compose --profile jobs run --rm pipeline test
```
Runs 42 tests (downloader, pipeline, services, addresses and API) in a separate scratch folder, so it never touches the data the site is serving.

### Step 8 — Optional: require a login
In `.env` set:
```
BASIC_AUTH_USER=planner
BASIC_AUTH_PASSWORD=choose-a-long-password
```
then `docker compose up -d` to apply. `/api/health` stays open for monitoring.

### Everyday commands
| Task | Command |
|---|---|
| See logs | `docker compose logs -f api` (one JSON line per request, with request ID and time) |
| Stop everything | `docker compose down` |
| Start again | `docker compose up -d` |
| Apply code changes | `docker compose up -d --build` |
| Delete all data too | `docker compose down -v` |
| API documentation | http://localhost:8080/api/docs |

---

## What the results do and don't mean

| The app checks | The app does **not** check |
|---|---|
| Zone (NRZ / GRZ) | Restrictive covenants, easements, other title restrictions |
| Overlays covering ≥ 5% of the lot. Heritage, flood, contamination, public acquisition and bushfire **exclude** a lot; character, design, development plan, vegetation, landscape and any unrecognised overlay **flag it for review** | Schedule-specific requirements inside each zone and overlay |
| Lot size, approximate width, existing building coverage (OpenStreetMap) | Ownership, lot consolidation, market feasibility |
| Walking distance along streets and paths to a metro station | Setbacks, site access, tree protection, design quality |
| Walking distance to the nearest supermarket (Coles, Woolworths, Aldi, IGA…), market, pharmacy, GP, school, childcare and park; share within reach feeds the ranking (Plan Melbourne's 20-minute neighbourhood ≈ 800 m) | Opening hours, size or capacity of each service |
| Straight-line distance to the nearest hospital (emergency department where recorded), police, fire and ambulance station | Emergency response times or road travel times |
| Recent clearing or construction seen by satellite (10 m pixels: lot-level indicator only) | Individual buildings, trees or pools from imagery |

- **Homes figure** = lot area × target density − 1 existing dwelling, for lots that pass every rule.
  It is theoretical capacity, not a forecast of what will be built.
- **Walking distance** is never silently swapped for straight-line distance. If the walking network can't
  be built, the app says so on screen and in the brief, and labels every distance as straight-line.
- **Warnings** appear above the results whenever something the numbers depend on is missing or doubtful.

## Running without Docker (for development)

Needs Python 3.12 or 3.13.
```bash
cd backend
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

sh run_pipeline.sh demo                   # or: sh run_pipeline.sh full   (Windows: run the python -m lines in run_pipeline.sh by hand)
SERVE_FRONTEND=../frontend uvicorn api.main:app --port 8000 --reload
```
Open http://localhost:8000. In this mode the API also serves the frontend, so there is no nginx layer
(no rate limits or security headers). Use Docker for anything other people will see.

---

## API

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | status, whether data is loaded, data version |
| GET | `/api/meta` | defaults, available layers, satellite dates, assumptions |
| GET | `/api/data-status` | each source's retrieval date, row count, state and warnings |
| POST | `/api/scenario` | warnings, totals, by-station table, exclusions, infrastructure demand, qualifying lots (GeoJSON) |
| POST | `/api/parcels/detail` | every piece of evidence behind one lot's result |
| GET | `/api/parcels/search?q=` | parcel IDs matching text |
| GET | `/api/layers/boundary`, `/stations`, `/changed`, `/satellite.png` | map layers |
| POST | `/api/export/csv`, `/geojson`, `/brief` | downloads for a scenario |

Scenario body example: `{"scenario": {"radius_m": 800, "min_lot_m2": 500, "dwellings_per_ha": 80}}` —
anything left out uses the defaults. Full schema at `/api/docs`.

---

## Later: putting it on a cloud server

When you're ready to share it beyond your computer:

1. **Server** — a Linux VM in an Australian region (AWS Sydney or Melbourne, Azure Australia East),
   4 vCPU / 16 GB RAM / 50 GB disk is plenty for one council.
2. **Install Docker** on it, copy the project across (or `git clone` your repository).
3. **Set `.env`** — turn on the login (Step 8) and set `HTTP_PORT=80`.
4. **HTTPS** — put a TLS-terminating proxy in front (Caddy, or nginx + Let's Encrypt), or use the cloud
   provider's load balancer with a managed certificate. Don't share the site over plain HTTP.
5. **Firewall** — open only ports 80/443; SSH from your IP only.
6. **Load data** — Steps 5–6 on the server; enable the scheduler.
7. **Backups** — snapshot the `housing-data` volume (or simply re-run the pipeline; all inputs are public).
8. **For a government pilot** — expect questions about hosting (IRAP-assessed cloud), the ACSC Essential Eight,
   accessibility (WCAG 2.2 AA) and data licensing. Raise these with the agency's IT team early.

---

## Data sources & licences

Vicmap Property, Planning & Address (DTP, CC BY 3.0 AU) · GTFS Schedule (Transport Victoria, CC BY 4.0) ·
OpenStreetMap (ODbL; also the source of shops, health, schools, parks and emergency services) · Overture Maps buildings and places (ODbL / CDLA-Permissive-2.0, includes Microsoft and Esri
footprints) · ABS Census 2021 & ASGS (CC BY 4.0) · Sentinel-2 ARD (Geoscience Australia / Digital
Earth Australia, CC BY 4.0) · MapLibre GL JS (BSD-3-Clause, bundled in `frontend/vendor`).
The aerial photo background (Esri World Imagery) is for visual checking only.

## Limitations

Theoretical capacity, not a forecast (ownership, consolidation, feasibility, setbacks and tree protection are
not modelled). Sentinel-2 pixels are 10 m, so lot-level satellite measures are indicative. OpenStreetMap
footprints may be incomplete. Census data is from 2021. Infrastructure rates in `backend/src/config.py` are
placeholders until confirmed with the water corporation, electricity distributor and Department of Education.

## Licence

Code: MIT (see `LICENSE`). Downloaded data stays under its publishers' licences listed above.
