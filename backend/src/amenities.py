"""Everyday needs (supermarkets, pharmacies, GPs, schools, childcare, parks, markets) and
emergency services (hospitals, police, fire, ambulance) near each lot.

Source: OpenStreetMap via Overpass (supermarkets carry their brand - Coles, Woolworths, Aldi, IGA),
with Overture Maps places as the fallback. Everyday needs are measured as a walk along streets;
emergency services as a straight line.
"""
from __future__ import annotations

import time

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

from . import config as C

HEADERS = {"User-Agent": "housing-opportunity-finder/0.5 (research prototype)"}
OUT = C.RAW / "amenities.parquet"


def classify(tags: dict) -> str | None:
    """OpenStreetMap tags -> amenity category key (first match wins)."""
    a, shop = tags.get("amenity"), tags.get("shop")
    if shop == "supermarket":
        return "supermarket"
    if a == "marketplace":
        return "market"
    if a == "pharmacy" or tags.get("healthcare") == "pharmacy":
        return "pharmacy"
    if a in ("doctors", "clinic"):
        return "gp"
    if a == "school":
        return "school"
    if a in ("kindergarten", "childcare"):
        return "childcare"
    if tags.get("leisure") == "park":
        return "park"
    if a == "hospital":
        return "hospital"
    if a == "police":
        return "police"
    if a == "fire_station":
        return "fire"
    if tags.get("emergency") == "ambulance_station":
        return "ambulance"
    return None


def _bbox(pad: float) -> tuple[float, float, float, float]:
    w, s, e, n = C.BBOX
    return w - pad, s - pad, e + pad, n + pad


# One small Overpass query per category: busy public servers answer small requests far more reliably.
OSM_SELECTORS = {
    "supermarket": ['nwr["shop"="supermarket"]'],
    "market":      ['nwr["amenity"="marketplace"]'],
    "pharmacy":    ['nwr["amenity"="pharmacy"]', 'nwr["healthcare"="pharmacy"]'],
    "gp":          ['nwr["amenity"~"^(doctors|clinic)$"]'],
    "school":      ['nwr["amenity"="school"]'],
    "childcare":   ['nwr["amenity"~"^(kindergarten|childcare)$"]'],
    "park":        ['way["leisure"="park"]', 'relation["leisure"="park"]'],
    "hospital":    ['nwr["amenity"="hospital"]'],
    "police":      ['nwr["amenity"="police"]'],
    "fire":        ['nwr["amenity"="fire_station"]'],
    "ambulance":   ['nwr["emergency"="ambulance_station"]'],
}


def _overpass_category(key: str) -> list[dict]:
    w, s, e, n = _bbox(C.AMENITY_PAD_DEG)
    b = f"({s},{w},{n},{e})"
    q = "[out:json][timeout:180];(" + "".join(f"{sel}{b};" for sel in OSM_SELECTORS[key]) + ");out center tags;"
    errors = []
    for attempt in range(2):
        for base in C.OVERPASS_API_BASES:
            try:
                r = requests.post(base + "/interpreter", data={"data": q}, headers=HEADERS, timeout=240)
                r.raise_for_status()
                elements = r.json().get("elements", [])
                break
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{base.split('/')[2]}: {str(ex)[:80]}")
        else:
            if attempt == 0:
                time.sleep(20)        # all busy: wait a little and go round once more
            continue
        break
    else:
        raise RuntimeError("; ".join(errors[-3:]))
    rows = []
    for el in elements:
        tags = el.get("tags", {})
        lat = el.get("lat", el.get("center", {}).get("lat"))
        lon = el.get("lon", el.get("center", {}).get("lon"))
        if lat is None:
            continue
        cat = classify(tags) or key
        if cat != key:      # e.g. a pharmacy inside a hospital query; keep each place in one category only
            continue
        rows.append({"category": key, "name": tags.get("name") or tags.get("brand") or C.AMENITIES[key]["label"],
                     "brand": tags.get("brand"),
                     "emergency_dept": tags.get("emergency") == "yes" if key == "hospital" else None,
                     "source": "OpenStreetMap", "lon": lon, "lat": lat})
    return rows


def _overture_category_of(row) -> tuple[str | None, str | None]:
    """Overture place -> (our category, its own label). Handles old and new category fields."""
    lookup = {oc: key for key, spec in C.AMENITIES.items() for oc in spec["overture"]}
    candidates = []
    for field in ("basic_category", "categories", "taxonomy"):
        v = row.get(field) if hasattr(row, "get") else None
        if isinstance(v, str):
            candidates.append(v)
        elif isinstance(v, dict):
            if v.get("primary"):
                candidates.append(v["primary"])
            for alt in (v.get("alternate") or v.get("hierarchy") or []):
                if isinstance(alt, str):
                    candidates.append(alt)
    for c in candidates:
        if c in lookup:
            return lookup[c], c
    return None, None


def _overture_places(keys: list[str]) -> list[dict]:
    from overturemaps import core
    print(f"  reading Overture Maps places for: {', '.join(C.AMENITIES[k]['label'] for k in keys)}...")
    g = core.geodataframe("place", bbox=_bbox(C.AMENITY_PAD_DEG))
    rows = []
    for rec in g.to_dict("records"):
        key, label = _overture_category_of(rec)
        if key not in keys:
            continue
        names = rec.get("names") or {}
        brand = rec.get("brand") or {}
        brand_names = brand.get("names") if isinstance(brand, dict) else None
        pt = rec["geometry"].representative_point()
        rows.append({"category": key,
                     "name": (names.get("primary") if isinstance(names, dict) else None) or C.AMENITIES[key]["label"],
                     "brand": brand_names.get("primary") if isinstance(brand_names, dict) else None,
                     "emergency_dept": True if label == "emergency_room" else None,
                     "source": "Overture Maps", "lon": pt.x, "lat": pt.y})
    return rows


def _to_gdf(rows: list[dict]) -> gpd.GeoDataFrame:
    df = pd.DataFrame(rows, columns=["category", "name", "brand", "emergency_dept", "source", "lon", "lat"])
    g = gpd.GeoDataFrame(df.drop(columns=["lon", "lat"]), geometry=gpd.points_from_xy(df.lon, df.lat), crs=C.CRS_WGS84)
    return g.to_crs(C.CRS_METRIC)


def fetch_amenities() -> tuple[int, str, dict]:
    """Download services category by category; returns (rows, source description, counts per category).

    Each category comes from OpenStreetMap if any Overpass server answers; categories that fail on
    every server are filled from Overture Maps places. The file is only replaced if something was found.
    """
    rows, from_osm, failed = [], [], {}
    for key in C.AMENITIES:
        try:
            got = _overpass_category(key)
            rows += got
            from_osm.append(key)
            print(f"    {C.AMENITIES[key]['label']}: {len(got)} (OpenStreetMap)")
        except Exception as e:  # noqa: BLE001
            failed[key] = str(e)
            print(f"    {C.AMENITIES[key]['label']}: OpenStreetMap servers busy ({str(e)[:100]})")
        time.sleep(1)          # be polite to the shared public servers
    from_overture = []
    if failed:
        try:
            got = _overture_places(list(failed))
            rows += got
            from_overture = sorted({r["category"] for r in got})
            for k in failed:
                print(f"    {C.AMENITIES[k]['label']}: {sum(r['category'] == k for r in got)} (Overture Maps)")
        except Exception as e:  # noqa: BLE001
            print(f"    Overture Maps places also failed: {str(e)[:200]}")
    g = _to_gdf(rows)
    if len(g) == 0:
        raise RuntimeError("no services could be downloaded - OpenStreetMap servers busy and Overture failed. "
                           "Try again later with: sh run_pipeline.sh refresh")
    tmp = OUT.with_suffix(".parquet.tmp")
    g.to_parquet(tmp)
    tmp.replace(OUT)
    counts = g["category"].value_counts().to_dict()
    parts = []
    if from_osm:
        parts.append(f"OpenStreetMap ({len(from_osm)} categories)")
    if from_overture:
        parts.append(f"Overture Maps ({', '.join(C.AMENITIES[k]['label'] for k in from_overture)})")
    print(f"  [ok] {OUT.name}: {len(g):,} services {counts}")
    return len(g), " + ".join(parts), counts


def attach_services(points: gpd.GeoSeries, net=None) -> tuple[pd.DataFrame, str]:
    """Nearest of each category for every point. Walking for daily needs when a network is available."""
    out = pd.DataFrame(index=points.index)
    if not OUT.exists():
        return out, "unavailable"
    am = gpd.read_parquet(OUT).to_crs(C.CRS_METRIC)
    method = "walking network" if net is not None else "straight line"
    if net is not None:
        p_nodes, p_snap = net.snap(points)
    pts = gpd.GeoDataFrame(geometry=list(points.values), index=points.index, crs=C.CRS_METRIC)
    for key, spec in C.AMENITIES.items():
        targets = am[am["category"] == key]
        if spec["kind"] == "emergency" and key == "hospital" and targets["emergency_dept"].fillna(False).any():
            # Prefer hospitals recorded as having an emergency department
            targets = targets[targets["emergency_dept"].fillna(False).astype(bool)]
        if len(targets) == 0:
            out[f"{key}_m"], out[f"{key}_name"] = np.nan, None
            continue
        if spec["kind"] != "emergency" and net is not None:
            d, names = net.nearest(targets, "name", p_nodes, p_snap, C.SERVICES_WALK_MAX_M)
            out[f"{key}_m"], out[f"{key}_name"] = d, names
        else:
            j = gpd.sjoin_nearest(pts, targets[["name", "geometry"]], distance_col="d")
            j = j[~j.index.duplicated()]
            out[f"{key}_m"] = j["d"].round(0)
            out[f"{key}_name"] = j["name"]
    return out, method
