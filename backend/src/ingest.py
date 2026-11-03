"""Step 1 - Download the raw data for the study area.

    python -m src.ingest                    # download anything missing
    python -m src.ingest --max-age-days 7   # also re-download anything older than 7 days (scheduler)

Sources (all open data):
  * Vicmap Property parcels      DTP, Vicmap open-data WFS          CC BY 3.0 AU
  * Vicmap Planning zones        DTP, WFS                           CC BY 3.0 AU
  * Vicmap Planning overlays     DTP, WFS                           CC BY 3.0 AU
  * Metro train stations         Transport Victoria GTFS Schedule   CC BY 4.0
  * Building footprints          Overture Maps (OSM + Microsoft + Esri), fallback OpenStreetMap   ODbL / CDLA
  * Everyday needs & emergency   OpenStreetMap via Overpass, fallback Overture Maps places        ODbL / CDLA
  * Lot-and-plan numbers (SPI)   DTP, Vicmap Property parcel detail (WFS)                         CC BY 3.0 AU
  * Street addresses             DTP, Vicmap Address (WFS)                                        CC BY 3.0 AU
  * ABS Census 2021 (SA1)        ABS DataPacks + ASGS boundaries    CC BY 4.0

Every download is written to a temporary file and only replaces the existing copy once it is
complete and non-empty, so a failed refresh never destroys working data. Each source's date,
size and any problem is recorded in status.json and shown in the app.
"""
from __future__ import annotations

import argparse
import io
import os
import time
import zipfile

import geopandas as gpd
import pandas as pd
import requests
from pyproj import Transformer
from shapely.geometry import Polygon

from . import config as C
from . import status

HEADERS = {"User-Agent": "housing-opportunity-finder/0.4 (research prototype)"}
OVERPASS_MIRRORS = [C.OVERPASS_URL, "https://overpass.kumi.systems/api/interpreter"]


class SourceError(RuntimeError):
    pass


def _bbox_3857():
    t = Transformer.from_crs(C.CRS_WGS84, "EPSG:3857", always_xy=True)
    minx, miny = t.transform(C.BBOX[0], C.BBOX[1])
    maxx, maxy = t.transform(C.BBOX[2], C.BBOX[3])
    return minx, miny, maxx, maxy


def _needs_download(path, max_age_days: float | None) -> bool:
    age = status.file_age_days(path)
    if age is None:
        return True
    if max_age_days is not None and age > max_age_days:
        print(f"  {path.name} is {age:.1f} days old - refreshing")
        return True
    print(f"  [skip] {path.name} is current ({age:.1f} days old)")
    return False


def _write_atomic(gdf: gpd.GeoDataFrame, out) -> None:
    if len(gdf) == 0:
        raise SourceError(f"{out.name}: download returned no features - keeping the previous copy")
    tmp = out.with_suffix(".parquet.tmp")
    gdf.to_parquet(tmp)
    os.replace(tmp, out)


def _get(url, **kw):
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=kw.pop("timeout", 300), **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"    retry {attempt + 1}/3 after error: {e}")
            time.sleep(5 * (attempt + 1))
    raise SourceError(f"{url}: {last}")


# ----------------------------------------------------------------------------- Vicmap WFS
def fetch_wfs_layer(layer: str, out_name: str, label: str, page_size: int = 10000, *,
                    property_names: list[str] | None = None, sort_by: str | None = "ufi",
                    with_geometry: bool = True, dedup_key: str | None = "auto",
                    min_complete: float = 1.0) -> int:
    """Page through a GeoServer WFS layer inside the study bbox (EPSG:3857 avoids axis-order issues).

    The server may return fewer features per request than asked for (Vicmap caps pages at 5,000),
    so paging continues until the server reports everything has been read or a page comes back empty.
    Results are sorted by a stable ID where the server allows it, so pages don't overlap or skip.
    property_names limits the columns fetched; with_geometry=False saves a plain table (much smaller).
    """
    out = C.RAW / f"{out_name}.parquet"
    minx, miny, maxx, maxy = _bbox_3857()
    base = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": layer,
            "outputFormat": "application/json", "srsName": "EPSG:3857",
            "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:3857", "count": page_size}
    if property_names:
        base["propertyName"] = ",".join(property_names)
    frames, start, total, first_ids = [], 0, None, set()
    while True:
        params = {**base, "startIndex": start, **({"sortBy": sort_by} if sort_by else {})}
        try:
            data = _get(C.VICMAP_WFS, params=params).json()
        except SourceError:
            if sort_by and start == 0:      # server won't sort on that field: page in natural order
                print(f"    (server can't sort by {sort_by}; paging in natural order)")
                sort_by = None
                continue
            raise
        feats = data.get("features", [])
        if total is None and isinstance(data.get("numberMatched"), int):
            total = data["numberMatched"]
        if not feats:
            break
        # Guard against a server that ignores startIndex and keeps returning the same page.
        page_key = (feats[0].get("id"), feats[-1].get("id"))
        if page_key in first_ids:
            raise SourceError(f"{layer}: server returned the same page twice; paging is not supported")
        first_ids.add(page_key)
        if with_geometry:
            frames.append(gpd.GeoDataFrame.from_features(feats, crs="EPSG:3857"))
        else:
            frames.append(pd.DataFrame([f.get("properties") or {} for f in feats]))
        start += len(feats)
        print(f"    {layer}: {start:,}" + (f" of {total:,}" if total else "") + " features")
        if total is not None and start >= total:
            break
    if not frames:
        raise SourceError(f"{layer} returned no features for the study area - check config.BBOX and the layer name")
    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.lower() for c in df.columns]
    key = next((k for k in ("ufi", "pfi") if k in df.columns), None) if dedup_key == "auto" else dedup_key
    if key and key in df.columns:
        df = df.drop_duplicates(subset=key)
    if total is not None and len(df) < total * min_complete:
        raise SourceError(f"{layer}: expected {total:,} features but received {len(df):,} - download incomplete")
    if with_geometry:
        gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:3857").to_crs(C.CRS_METRIC)
        _write_atomic(gdf, out)
        n = len(gdf)
    else:
        if len(df) == 0:
            raise SourceError(f"{out.name}: download returned no rows - keeping the previous copy")
        tmp = out.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        os.replace(tmp, out)
        n = len(df)
    print(f"  [ok] {out.name}: {n:,} " + ("features" if with_geometry else "rows (attributes only)"))
    return n


def describe_layer(layer: str) -> dict[str, str]:
    """Column name -> type for a WFS layer (lower-cased names)."""
    desc = _get(C.VICMAP_WFS, params={"service": "WFS", "version": "2.0.0", "request": "DescribeFeatureType",
                                     "typeNames": layer, "outputFormat": "application/json"}).json()
    return {p["name"]: str(p.get("type", "")) for ft in desc.get("featureTypes", []) for p in ft.get("properties", [])}


# ----------------------------------------------------------------------------- GTFS
def _gtfs_urls() -> list[str]:
    urls = [C.GTFS_URL]
    try:
        res = requests.get(C.GTFS_CKAN, headers=HEADERS, timeout=60).json()["result"]["resources"]
        urls += [r["url"] for r in res if str(r.get("url", "")).lower().endswith(".zip")]
    except Exception as e:  # noqa: BLE001
        print(f"    (could not ask the open-data portal for the GTFS link: {e})")
    return list(dict.fromkeys(urls))


def fetch_train_stations() -> tuple[int, str]:
    """Metro train stations from the GTFS Schedule (folder 2 = metropolitan train)."""
    out = C.RAW / "stations.parquet"
    zpath, tmpzip = C.RAW / "gtfs.zip", C.RAW / "gtfs.zip.tmp"
    used, errors = None, []
    for url in _gtfs_urls():
        try:
            print(f"  downloading GTFS from {url} (~200-300 MB)...")
            with requests.get(url, headers=HEADERS, stream=True, timeout=600) as r:
                r.raise_for_status()
                with open(tmpzip, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            zipfile.ZipFile(tmpzip).testzip()
            os.replace(tmpzip, zpath)
            used = url
            break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
    if used is None:
        raise SourceError("GTFS download failed: " + " | ".join(errors))

    with zipfile.ZipFile(zpath) as outer:
        inner_name = next((n for n in outer.namelist() if n.strip("/").startswith("2/") and n.endswith(".zip")), None)
        if inner_name is None:
            raise SourceError(f"GTFS zip has no metro-train folder '2/'. Contents: {outer.namelist()[:12]}")
        with zipfile.ZipFile(io.BytesIO(outer.read(inner_name))) as inner:
            stops = pd.read_csv(inner.open("stops.txt"), dtype=str)
    stops["stop_lat"] = stops["stop_lat"].astype(float)
    stops["stop_lon"] = stops["stop_lon"].astype(float)
    if "location_type" in stops and (stops["location_type"] == "1").any():
        stops = stops[stops["location_type"] == "1"]          # one point per station
    else:
        stops = stops.drop_duplicates("stop_name")
    gdf = gpd.GeoDataFrame(stops[["stop_id", "stop_name"]],
                           geometry=gpd.points_from_xy(stops["stop_lon"], stops["stop_lat"]), crs=C.CRS_WGS84)
    pad, b = 0.02, C.BBOX   # ~2 km margin so edge lots still find their nearest station
    gdf = gdf.cx[b[0] - pad:b[2] + pad, b[1] - pad:b[3] + pad].to_crs(C.CRS_METRIC)
    _write_atomic(gdf, out)
    print(f"  [ok] {out.name}: {len(gdf)} stations")
    return len(gdf), used


# ----------------------------------------------------------------------------- buildings
def _overture_buildings() -> gpd.GeoDataFrame:
    """Overture Maps buildings: OpenStreetMap merged with Microsoft and Esri machine-detected footprints."""
    from overturemaps import core
    print("  reading Overture Maps buildings for the study area (a few minutes)...")
    gdf = core.geodataframe("building", bbox=tuple(C.BBOX))
    if gdf.crs is None:
        gdf = gdf.set_crs(C.CRS_WGS84)
    keep = [c for c in ("id", "sources", "height", "num_floors") if c in gdf.columns]
    gdf = gdf[keep + ["geometry"]].copy()
    if "sources" in gdf:   # summarise provenance, e.g. "OpenStreetMap" / "Microsoft ML Buildings"
        gdf["source"] = gdf["sources"].map(
            lambda s: ", ".join(sorted({d.get("dataset", "?") for d in s})) if s is not None and len(s) else "unknown")
        gdf = gdf.drop(columns="sources")
    return gdf


def _osm_buildings() -> gpd.GeoDataFrame:
    w, s, e, n = C.BBOX
    query = f"""[out:json][timeout:600];(way["building"]({s},{w},{n},{e}););out geom;"""
    errors = []
    for url in OVERPASS_MIRRORS:
        try:
            print(f"  querying {url} for buildings (a few minutes)...")
            r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=900)
            r.raise_for_status()
            elements = r.json().get("elements", [])
            break
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{url}: {ex}")
    else:
        raise SourceError("Overpass failed: " + " | ".join(errors))
    polys, ids = [], []
    for el in elements:
        pts = [(p["lon"], p["lat"]) for p in el.get("geometry", [])]
        if len(pts) >= 4:
            polys.append(Polygon(pts))
            ids.append(f"osm:{el['id']}")
    return gpd.GeoDataFrame({"id": ids, "source": "OpenStreetMap"}, geometry=polys, crs=C.CRS_WGS84)


def fetch_buildings() -> tuple[int, str]:
    out = C.RAW / "buildings.parquet"
    attempts = [("overture", _overture_buildings), ("osm", _osm_buildings)]
    if C.BUILDINGS_SOURCE == "osm":
        attempts.reverse()
    errors = []
    for name, fn in attempts:
        try:
            gdf = fn()
            break
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            print(f"    {name} buildings failed: {e}")
    else:
        raise SourceError("All building sources failed: " + " | ".join(errors))
    gdf = gdf[gdf.geometry.notna()].to_crs(C.CRS_METRIC)
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"]) & gdf.is_valid & (gdf.area > 10)]
    _write_atomic(gdf, out)
    mix = gdf["source"].value_counts().head(4).to_dict() if "source" in gdf else {}
    print(f"  [ok] {out.name}: {len(gdf):,} buildings from {name} {mix}")
    label_url = "Overture Maps (s3://overturemaps-us-west-2)" if name == "overture" else C.OVERPASS_URL
    return len(gdf), label_url


# ----------------------------------------------------------------------------- lot-and-plan numbers, addresses
_CAPS: set | None = None


def resolve_layer(preferred: str, keywords: tuple[str, ...]) -> str:
    """Use the configured layer name if the service offers it; otherwise find a similarly named one."""
    global _CAPS
    if _CAPS is None:
        import xml.etree.ElementTree as ET
        xml = _get(C.VICMAP_WFS, params={"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"},
                   timeout=180).text
        _CAPS = {el.text for el in ET.fromstring(xml).iter() if el.tag.endswith("}Name") and el.text}
    if preferred in _CAPS:
        return preferred
    matches = sorted(n for n in _CAPS if all(k in n.lower() for k in keywords))
    if not matches:
        raise SourceError(f"layer {preferred} not offered and nothing matches {keywords}")
    print(f"    ({preferred} not offered; using {matches[0]})")
    return matches[0]


def fetch_parcel_detail() -> tuple[int, str, str]:
    """Lot-and-plan numbers. Downloads only the SPI and parcel ID columns when the layer has a shared ID,
    which is a small table instead of ~200,000 polygons; otherwise SPI plus outlines (matched by location)."""
    layer = resolve_layer(C.LAYER_PARCEL_DETAIL, ("parcel", "mp"))
    cols = describe_layer(layer)
    lower = {c.lower(): c for c in cols}
    print(f"    {layer} columns: {sorted(cols)}")
    spi = next((lower[c] for c in ("parcel_spi", "spi") if c in lower), None) or \
        next((c for c in cols if "spi" in c.lower()), None)
    if spi is None:
        raise SourceError(f"{layer} has no SPI column. Columns: {sorted(cols)}")
    ids = [c for c, t in cols.items() if c != spi and not t.lower().startswith("gml:")
           and ("pfi" in c.lower() or "ufi" in c.lower())]
    key = next((lower[c] for c in ("ufi", "parcel_pfi", "pfi") if c in lower and lower[c] in ids), ids[0] if ids else None)
    geom = next((c for c, t in cols.items() if t.lower().startswith("gml:")), None)
    if ids:
        n = fetch_wfs_layer(layer, "parcel_detail", "parcel detail", property_names=ids + [spi], sort_by=key,
                            with_geometry=False, dedup_key=None, min_complete=0.99)
        note = f"SPI from '{spi}'; ID columns {ids} kept so lots can be matched on whichever fits."
    elif geom:
        n = fetch_wfs_layer(layer, "parcel_detail", "parcel detail", property_names=[spi, geom], sort_by=spi,
                            dedup_key=None, min_complete=0.99)
        note = f"SPI from '{spi}', matched to lots by location."
    else:
        raise SourceError(f"{layer} has neither a parcel ID nor a geometry column. Columns: {sorted(cols)}")
    return n, C.VICMAP_WFS + "?typeNames=" + layer, note


def fetch_addresses() -> tuple[int, str]:
    layer = resolve_layer(C.LAYER_ADDRESS, ("address",))
    n = fetch_wfs_layer(layer, "addresses", "addresses")
    return n, C.VICMAP_WFS + "?typeNames=" + layer


# ----------------------------------------------------------------------------- orchestration
SOURCES = [
    ("parcels", "Lot boundaries (Vicmap Property)", "updated as subdivisions are registered",
     lambda: (fetch_wfs_layer(C.LAYER_PARCELS, "parcels", "parcels"), C.VICMAP_WFS + "?typeNames=" + C.LAYER_PARCELS)),
    ("zones", "Planning zones (Vicmap Planning)", "updated when planning scheme amendments are approved",
     lambda: (fetch_wfs_layer(C.LAYER_ZONES, "zones", "zones"), C.VICMAP_WFS + "?typeNames=" + C.LAYER_ZONES)),
    ("overlays", "Planning overlays (Vicmap Planning)", "updated when planning scheme amendments are approved",
     lambda: (fetch_wfs_layer(C.LAYER_OVERLAYS, "overlays", "overlays"), C.VICMAP_WFS + "?typeNames=" + C.LAYER_OVERLAYS)),
    ("stations", "Train stations (Transport Victoria GTFS)", "weekly", fetch_train_stations),
    ("buildings", "Building footprints (Overture Maps: OpenStreetMap + Microsoft + Esri)",
     "monthly Overture releases", fetch_buildings),
    ("amenities", "Shops, health, schools, parks and emergency services", "continuously edited by volunteers",
     lambda: _fetch_services()),
    ("parcel_detail", "Lot-and-plan numbers (Vicmap Property)", "updated as subdivisions are registered",
     fetch_parcel_detail),
    ("addresses", "Street addresses (Vicmap Address)", "updated weekly", fetch_addresses),
]


def _fetch_services():
    from .amenities import fetch_amenities
    rows, src, counts = fetch_amenities()
    missing = [C.AMENITIES[k]["label"] for k in C.AMENITIES if counts.get(k, 0) == 0]
    note = f"Source: {src}." + (f" None found for: {', '.join(missing)}." if missing else "")
    return rows, src, note


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-days", type=float, default=None,
                    help="re-download sources older than this many days")
    a = ap.parse_args()
    C.RAW.mkdir(parents=True, exist_ok=True)

    failed = []
    for i, (name, label, updates, fn) in enumerate(SOURCES, 1):
        print(f"{i}/{len(SOURCES) + 1} {label}")
        path = C.RAW / f"{name}.parquet"
        if not _needs_download(path, a.max_age_days):
            continue
        try:
            res = fn()
            rows, url, note = (res + ("",))[:3]
            status.source(name, label=label, url=url, rows=rows, updates=updates, note=note,
                          state="warning" if "None found" in note else "ok")
        except Exception as e:  # noqa: BLE001
            failed.append(name)
            had_copy = path.exists()
            note = f"Download failed: {e}." + (" Using the previous copy." if had_copy else " No copy available.")
            print(f"  [FAILED] {note}")
            status.source(name, label=label, state="warning" if had_copy else "failed", note=note, updates=updates)

    print(f"{len(SOURCES) + 1}/{len(SOURCES) + 1} ABS Census 2021 (SA1)")
    try:
        from .census import OUT, fetch_census
        if not OUT.exists():
            fetch_census()
        rows = len(gpd.read_parquet(OUT))
        status.source("census", label="Rent, income, household size (ABS Census 2021)", url=C.ABS_GCP_URL,
                      rows=rows, updates="every 5 years (2021 figures)",
                      retrieved_at=status.load()["sources"].get("census", {}).get("retrieved_at"))
    except Exception as e:  # noqa: BLE001
        print(f"  [FAILED] census: {e}")
        status.source("census", label="Rent, income, household size (ABS Census 2021)", state="failed",
                      note=f"Download failed: {e}", updates="every 5 years")

    if failed:
        print(f"\nSome sources failed: {', '.join(failed)}. Run `sh run_pipeline.sh verify` to diagnose.")
    print("Done. Next: python -m src.satellite, then python -m src.process")


if __name__ == "__main__":
    main()
