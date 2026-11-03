"""Smart Housing & Zoning Optimiser - REST API.

Run locally:   uvicorn api.main:app --reload --port 8000     (from the backend/ folder)
Interactive docs: http://localhost:8000/api/docs
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import fields

import geopandas as gpd
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from src import config as C
from src.infrastructure import demand, demand_by_area
from src.report import brief
from src import status as data_status
from src.scoring import Scenario, score, summarise

from .store import clean, store

VERSION = "0.3.0"
log = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(message)s")



@asynccontextmanager
async def lifespan(_app):
    # Load data in the background so the server (and its health check) is up immediately.
    threading.Thread(target=store.get, daemon=True).start()
    yield


app = FastAPI(title="Smart Housing & Zoning Optimiser API", version=VERSION, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

# ---------------------------------------------------------------- API middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
origins = [o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o]
if origins:  # only needed when the frontend is served from a different origin than the API
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["*"])


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Request ID + timing + one structured log line per request."""
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001
        log.exception(json.dumps({"rid": rid, "path": request.url.path, "error": "unhandled"}))
        response = JSONResponse({"detail": "Internal error. Quote this ID when reporting it.", "request_id": rid},
                                status_code=500)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    response.headers["X-Request-ID"] = rid
    response.headers["Server-Timing"] = f"app;dur={ms}"
    log.info(json.dumps({"rid": rid, "method": request.method, "path": request.url.path,
                         "status": response.status_code, "ms": ms}))
    return response


# ---------------------------------------------------------------- models
class ScenarioIn(BaseModel):
    radius_m: float = Field(C.DEFAULT_RADIUS_M, ge=100, le=3000)
    min_lot_m2: float = Field(C.DEFAULT_MIN_LOT_M2, ge=50, le=10000)
    min_width_m: float = Field(C.DEFAULT_MIN_WIDTH_M, ge=0, le=100)
    max_coverage: float = Field(C.DEFAULT_MAX_COVERAGE, ge=0, le=1)
    dwellings_per_ha: float = Field(C.DEFAULT_DWELLINGS_PER_HA, ge=10, le=500)
    zones: list[str] = Field(default_factory=lambda: list(C.CANDIDATE_ZONE_PREFIXES))
    exclude_overlays: bool = True
    exclude_review_overlays: bool = False
    use_walking: bool = True
    exclude_recent_change: bool = True
    max_green_frac: float = Field(1.0, ge=0, le=1)
    services_walk_m: float = Field(C.DEFAULT_SERVICES_WALK_M, ge=100, le=3000)
    max_supermarket_m: float = Field(0, ge=0, le=3000)
    w_proximity: float = Field(0.30, ge=0, le=1)
    w_lot_size: float = Field(0.20, ge=0, le=1)
    w_open_land: float = Field(0.20, ge=0, le=1)
    w_demand: float = Field(0.15, ge=0, le=1)
    w_services: float = Field(0.15, ge=0, le=1)

    def to_scenario(self) -> Scenario:
        d = self.model_dump()
        d["zones"] = tuple(z.upper() for z in d["zones"] if z) or ("NRZ",)
        return Scenario(**{f.name: d[f.name] for f in fields(Scenario)})


class ScenarioRequest(BaseModel):
    scenario: ScenarioIn = Field(default_factory=ScenarioIn)
    max_lots: int = Field(5000, ge=0, le=50000)


class ParcelRequest(BaseModel):
    parcel_id: str
    scenario: ScenarioIn = Field(default_factory=ScenarioIn)


LOT_PROPS = ["parcel_id", "address", "lot_plan", "zone", "area_m2", "coverage", "station", "access_m", "green_frac", "score", "new_dwellings",
             "needs_review", "services_met"]
SERVICE_COLS = [f"{k}_{x}" for k in C.AMENITIES for x in ("name", "m")]
EXPORT_COLS = ["parcel_id", "spi", "lot_plan", "address", "address_count", "zone", "area_m2", "lot_width_m", "built_m2", "coverage", "station", "access_m",
               "dist_station_m", "walk_status", "overlays_exclude", "overlays_review", "needs_review",
               "overlays", "sa1", "median_rent_wk", "rent_to_income", "ndvi_mean", "green_frac", "d_ndvi",
               "d_ndbi", "services_met", "score", "new_dwellings"] + SERVICE_COLS


# ---------------------------------------------------------------- helpers
def snapshot():
    snap = store.get()
    if snap.parcels is None:
        raise HTTPException(503, "No processed data yet. Run the pipeline (see README step 4).")
    return snap


def run(s: ScenarioIn):
    snap = snapshot()
    sc = s.to_scenario()
    df = score(snap.parcels, sc)
    return snap, sc, df


def features(snap, rows, props) -> dict:
    g = gpd.GeoDataFrame(rows[[p for p in props if p in rows]].round(3),
                         geometry=snap.geom_wgs.loc[rows.index], crs=C.CRS_WGS84)
    return json.loads(g.to_json(drop_id=True, na="null"))


def records(frame) -> list[dict]:
    return [{k: clean(v) for k, v in r.items()} for r in frame.reset_index().to_dict("records")]


# ---------------------------------------------------------------- endpoints
@app.get("/api/health")
def health():
    snap = store.peek()
    return {"status": "ok", "version": VERSION, "data_loaded": snap.parcels is not None,
            "parcels": 0 if snap.parcels is None else len(snap.parcels), "data_version": snap.version}


@app.get("/api/meta")
def meta():
    snap = snapshot()
    zones = sorted({z[:3] for z in snap.parcels["zone"].dropna().unique() if z[:3] in ("NRZ", "GRZ", "RGZ", "MUZ")})
    return {"center": snap.center, "flags": snap.flags, "satellite": snap.sat_meta,
            "zones_available": zones, "defaults": ScenarioIn().model_dump(),
            "infrastructure_assumptions": C.INFRA_ASSUMPTIONS, "parcels": len(snap.parcels),
            "study_area": "Merri-bek City Council",
            "amenities": {"categories": {k: {x: v[x] for x in ("label", "group", "kind")} for k, v in C.AMENITIES.items()},
                          "groups": C.AMENITY_GROUPS}}


@app.get("/api/data-status")
def data_status_endpoint():
    """Where each dataset came from, when it was retrieved, row counts and any problems."""
    st = data_status.load()
    states = [v.get("state") for v in st.get("sources", {}).values()]
    overall = "failed" if "failed" in states else "warning" if ("warning" in states or
                                                                st.get("processing", {}).get("warnings")) else "ok"
    return {"overall": overall, **st}


@app.get("/api/layers/boundary")
def boundary():
    return snapshot().boundary


@app.get("/api/layers/stations")
def stations():
    return snapshot().stations


@app.get("/api/layers/amenities")
def amenities_layer():
    """Shops, health, schools, parks and emergency services as points."""
    from src.amenities import OUT
    if not OUT.exists():
        return {"type": "FeatureCollection", "features": []}
    am = gpd.read_parquet(OUT).to_crs(C.CRS_WGS84)
    am["label"] = am["category"].map(lambda k: C.AMENITIES[k]["label"])
    am["group"] = am["category"].map(lambda k: C.AMENITIES[k]["group"])
    cols = [c for c in ("category", "label", "group", "name", "brand", "emergency_dept") if c in am]
    return json.loads(am[cols + ["geometry"]].to_json(drop_id=True, na="null"))


@app.get("/api/layers/changed")
def changed():
    snap = snapshot()
    p = snap.parcels
    if "recent_change" not in p:
        return {"type": "FeatureCollection", "features": []}
    rows = p[p["recent_change"].fillna(False).astype(bool)]
    return features(snap, rows, ["parcel_id", "d_ndvi", "d_ndbi"])


@app.get("/api/layers/satellite.png")
def satellite_png():
    path = C.PROCESSED / "sat_truecolour.png"
    if not path.exists():
        raise HTTPException(404, "No satellite image yet. Run the satellite step of the pipeline.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


@app.post("/api/scenario")
def scenario(req: ScenarioRequest):
    snap, sc, df = run(req.scenario)
    summ = summarise(df)
    elig = df[df["eligible"]].sort_values("score", ascending=False)
    infra = demand(elig)
    return {
        "warnings": df.attrs.get("warnings", []) + data_status.load().get("processing", {}).get("warnings", []),
        "summary": {k: clean(summ[k]) for k in ("eligible_lots", "new_dwellings", "needs_review", "land_ha",
                                                 "open_land_ha")}
                   | {"services_access": {k: round(v, 3) for k, v in summ.get("services_access", {}).items()}}
                   | {"access_method": df.attrs.get("access_method"), "lots_returned": min(len(elig), req.max_lots)},
        "by_station": records(summ["by_station"]),
        "exclusions": [{"reason": k, "lots": int(v)} for k, v in summ["exclusions"].items()],
        "infrastructure": {k: clean(v) for k, v in infra.items() if k != "assumptions"},
        "infrastructure_by_station": records(demand_by_area(elig, "station")) if len(elig) else [],
        "assumptions": infra["assumptions"],
        "lots": features(snap, elig.head(req.max_lots), LOT_PROPS),
    }


@app.get("/api/parcels/search")
def search(q: str, limit: int = 10):
    """Find lots by street address, lot-and-plan number (12\\LP12345 or 'Lot 12 on LP12345') or ID."""
    snap = snapshot()
    p = snap.parcels
    q = q.strip()
    if len(q) < 2:
        return {"results": []}
    mask = p["parcel_id"].astype(str).str.contains(q, case=False, regex=False, na=False)
    for col in ("address", "spi", "lot_plan"):
        if col in p:
            mask |= p[col].astype(str).str.contains(q, case=False, regex=False, na=False)
    rows = p.loc[mask].head(min(limit, 25))
    pts = snap.geom_wgs.loc[rows.index].representative_point()
    return {"results": [
        {"parcel_id": r["parcel_id"], "address": clean(r.get("address")) or "",
         "lot_plan": clean(r.get("lot_plan")) or "", "lon": round(pt.x, 6), "lat": round(pt.y, 6)}
        for (_, r), pt in zip(rows.iterrows(), pts)]}


@app.post("/api/parcels/detail")
def parcel_detail(req: ParcelRequest):
    snap, sc, df = run(req.scenario)
    rows = df[df["parcel_id"] == req.parcel_id]
    if rows.empty:
        raise HTTPException(404, f"Parcel {req.parcel_id} is not in the study area.")
    r = rows.iloc[0]
    ev = {k: clean(r[k]) for k in EXPORT_COLS + ["walk_min", "median_hh_income_wk", "avg_hh_size", "recent_change",
                                                 "sat_low_confidence", "f_proximity", "f_lot_size", "f_open_land",
                                                 "f_demand", "f_services", "eligible", "excluded_reason"] if k in r}
    ev["access_method"] = df.attrs.get("access_method")
    for k in ("spi", "lot_plan", "address", "address_count", "walk_status", "lot_width_m", "overlays", "overlays_exclude", "overlays_review", "needs_review",
              "services_met", "services_method"):
        if k in r:
            ev[k] = clean(r[k])
    target = req.scenario.services_walk_m
    ev["services"] = [
        {"key": k, "label": v["label"], "group": v["group"], "kind": v["kind"],
         "name": clean(r.get(f"{k}_name")), "distance_m": clean(r.get(f"{k}_m")),
         "method": "straight line" if v["kind"] == "emergency" else r.get("services_method", "straight line"),
         "within_reach": (None if v["kind"] != "daily" or r.get(f"{k}_m") is None or pd.isna(r.get(f"{k}_m"))
                          else bool(r.get(f"{k}_m") <= target))}
        for k, v in C.AMENITIES.items() if f"{k}_m" in r]
    feat = features(snap, rows, ["parcel_id"])["features"][0]
    return {"parcel": ev, "geometry": feat["geometry"]}


@app.post("/api/export/{fmt}")
def export(fmt: str, req: ScenarioRequest):
    snap, sc, df = run(req.scenario)
    elig = df[df["eligible"]].sort_values("score", ascending=False)
    cols = [c for c in EXPORT_COLS if c in elig]
    if fmt == "csv":
        return Response(elig[cols].round(4).to_csv(index=False), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="eligible_lots.csv"'})
    if fmt == "geojson":
        return Response(json.dumps(features(snap, elig, cols)), media_type="application/geo+json",
                        headers={"Content-Disposition": 'attachment; filename="eligible_lots.geojson"'})
    if fmt == "brief":
        md = brief(df, summarise(df), sc, demand(elig), snap.sat_meta)
        return PlainTextResponse(md, media_type="text/markdown",
                                 headers={"Content-Disposition": 'attachment; filename="scenario_brief.md"'})
    raise HTTPException(400, "Format must be csv, geojson or brief.")


# ---------------------------------------------------------------- optional: serve the frontend
# For running without Docker/nginx:  SERVE_FRONTEND=../frontend uvicorn api.main:app --port 8000
if os.environ.get("SERVE_FRONTEND"):
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=os.environ["SERVE_FRONTEND"], html=True), name="frontend")
