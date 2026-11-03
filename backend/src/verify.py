"""Check every data source is reachable and still has the structure the pipeline expects.

    python -m src.verify          (or: sh run_pipeline.sh verify)

Takes about a minute and downloads only a few kilobytes. Run it before the full pipeline,
and whenever a download fails. Results are also saved to status.json and shown in the app.
"""
from __future__ import annotations

import datetime as dt
import sys
import xml.etree.ElementTree as ET

import requests

from . import config as C
from . import status
from .ingest import HEADERS, _bbox_3857

REQUIRED_FIELDS = {
    C.LAYER_PARCELS: [("parcel id", ["parcel_spi", "spi", "parcel_pfi", "pfi", "ufi"])],
    C.LAYER_ZONES: [("zone code", ["zone_code"]), ("council name", ["lga", "lga_name", "lga_name_full"])],
    C.LAYER_OVERLAYS: [("overlay code", ["zone_code", "overlay_code"])],
}

results: list[dict] = []


def check(name: str, required: bool = True):
    def wrap(fn):
        def run():
            try:
                detail = fn()
                results.append({"check": name, "ok": True, "required": required, "detail": detail})
            except Exception as e:  # noqa: BLE001
                results.append({"check": name, "ok": False, "required": required, "detail": str(e)[:300]})
        return run
    return wrap


def _get(url, **kw):
    r = requests.get(url, headers=HEADERS, timeout=kw.pop("timeout", 60), **kw)
    r.raise_for_status()
    return r


# ------------------------------------------------------------------ Vicmap
@check("Vicmap WFS: service and layer names")
def vicmap_capabilities():
    xml = _get(C.VICMAP_WFS, params={"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"},
               timeout=120).text
    names = {el.text for el in ET.fromstring(xml).iter() if el.tag.endswith("}Name") and el.text}
    missing = [l for l in (C.LAYER_PARCELS, C.LAYER_ZONES, C.LAYER_OVERLAYS) if l not in names]
    if missing:
        close = sorted(n for n in names if any(k in n.lower() for k in ("parcel", "zone", "overlay")))[:15]
        raise RuntimeError(f"layer(s) not offered: {missing}. Similar layers available: {close}")
    return "all three layers offered"


def _layer_check(layer):
    @check(f"Vicmap {layer.split(':')[-1]}: fields and a sample feature")
    def run():
        desc = _get(C.VICMAP_WFS, params={"service": "WFS", "version": "2.0.0", "request": "DescribeFeatureType",
                                         "typeNames": layer, "outputFormat": "application/json"}).json()
        props = {p["name"].lower() for ft in desc.get("featureTypes", []) for p in ft.get("properties", [])}
        problems = [f"no {what} field (looked for {opts})" for what, opts in REQUIRED_FIELDS[layer]
                    if not props.intersection(opts)]
        if problems:
            raise RuntimeError("; ".join(problems) + f". Fields available: {sorted(props)}")
        minx, miny, maxx, maxy = _bbox_3857()
        feat = _get(C.VICMAP_WFS, params={"service": "WFS", "version": "2.0.0", "request": "GetFeature",
                                         "typeNames": layer, "outputFormat": "application/json", "count": 1,
                                         "srsName": "EPSG:3857", "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:3857"}).json()
        if not feat.get("features"):
            raise RuntimeError("no features returned inside the study area")
        sample = {k: v for k, v in feat["features"][0]["properties"].items() if k.lower() in
                  {o for _, opts in REQUIRED_FIELDS[layer] for o in opts}}
        return f"fields ok; sample {sample}"
    return run


@check("Vicmap parcels: paging through the full study area")
def vicmap_paging():
    minx, miny, maxx, maxy = _bbox_3857()
    base = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": C.LAYER_PARCELS,
            "outputFormat": "application/json", "srsName": "EPSG:3857", "count": 10000, "sortBy": "ufi",
            "bbox": f"{minx},{miny},{maxx},{maxy},EPSG:3857", "propertyName": "ufi"}
    p1 = _get(C.VICMAP_WFS, params={**base, "startIndex": 0}, timeout=180).json()
    n1, total = len(p1.get("features", [])), p1.get("numberMatched")
    p2 = _get(C.VICMAP_WFS, params={**base, "startIndex": n1}, timeout=180).json()
    f1 = p1["features"][0]["properties"].get("ufi") if n1 else None
    f2 = p2["features"][0]["properties"].get("ufi") if p2.get("features") else None
    if n1 and f1 == f2:
        raise RuntimeError("second page starts with the same feature as the first - paging not working")
    return (f"server returns up to {n1:,} per page; {total if isinstance(total, int) else 'unknown'} parcels in "
            f"the study area bounding box; second page differs from the first")


def _identity_check(preferred, keywords, want, label):
    @check(f"Vicmap {label}: layer and columns", required=False)
    def run():
        from .ingest import resolve_layer
        layer = resolve_layer(preferred, keywords)
        desc = _get(C.VICMAP_WFS, params={"service": "WFS", "version": "2.0.0", "request": "DescribeFeatureType",
                                         "typeNames": layer, "outputFormat": "application/json"}).json()
        props = sorted({p["name"].lower() for ft in desc.get("featureTypes", []) for p in ft.get("properties", [])})
        hits = [w for w in want if any(w in p_ for p_ in props)]
        if not hits:
            raise RuntimeError(f"{layer} has none of {want}. Columns: {props}")
        return f"{layer}; useful columns found: {hits}; all columns: {props[:25]}"
    return run


# ------------------------------------------------------------------ transport
@check("Transport Victoria GTFS: download link")
def gtfs():
    from .ingest import _gtfs_urls
    notes = []
    for url in _gtfs_urls():
        try:
            r = requests.head(url, headers=HEADERS, timeout=60, allow_redirects=True)
            if r.status_code == 405:   # some servers refuse HEAD
                r = requests.get(url, headers={**HEADERS, "Range": "bytes=0-1023"}, timeout=60, stream=True)
            r.raise_for_status()
            size = int(r.headers.get("Content-Length", 0)) / 1e6
            return f"{url} reachable" + (f", {size:.0f} MB" if size > 1 else "") + \
                   (f", last modified {r.headers['Last-Modified']}" if "Last-Modified" in r.headers else "")
        except Exception as e:  # noqa: BLE001
            notes.append(f"{url}: {e}")
    raise RuntimeError(" | ".join(notes) or "no candidate URLs")


# ------------------------------------------------------------------ OSM
@check("OpenStreetMap Overpass (walking network, services; fallback for buildings)", required=False)
def overpass():
    w, s = C.BBOX[0], C.BBOX[1]
    q = f'[out:json][timeout:60];way["building"]({s + 0.04},{w + 0.04},{s + 0.045},{w + 0.045});out count;'
    errors = []
    for base in C.OVERPASS_API_BASES:       # public servers are often busy; any one working is enough
        try:
            r = requests.post(base + "/interpreter", data={"data": q}, headers=HEADERS, timeout=90)
            r.raise_for_status()
            n = r.json()["elements"][0]["tags"].get("ways", "?")
            note = f" (first {len(errors)} server(s) busy)" if errors else ""
            return f"{n} buildings in a 500 m test square via {base}{note}"
        except Exception as e:  # noqa: BLE001
            errors.append(f"{base}: {str(e)[:100]}")
    raise RuntimeError("all Overpass servers busy or unreachable - usually temporary; walking network and "
                       "services will retry and fall back. " + " | ".join(errors))


@check("Overture Maps buildings: small-area read", required=False)
def overture():
    from overturemaps import core
    w, s_ = C.BBOX[0] + 0.07, C.BBOX[1] + 0.05
    gdf = core.geodataframe("building", bbox=(w, s_, w + 0.004, s_ + 0.003))
    if len(gdf) == 0:
        raise RuntimeError("no buildings returned for a test square inside the study area")
    srcs = {}
    if "sources" in gdf:
        for lst in gdf["sources"]:
            for d in (lst if lst is not None else []):
                srcs[d.get("dataset", "?")] = srcs.get(d.get("dataset", "?"), 0) + 1
    return f"{len(gdf)} buildings in a ~350 m test square; sources {srcs}"


@check("OpenStreetMap services: supermarkets, pharmacies, hospitals", required=False)
def services():
    w, s_, e, n = C.BBOX
    b = f"({s_},{w},{n},{e})"
    q = (f'[out:json][timeout:90];(nwr["shop"="supermarket"]{b};)->.a;(nwr["amenity"="pharmacy"]{b};)->.b;'
         f'(nwr["amenity"="hospital"]{b};)->.c;.a out count;.b out count;.c out count;')
    last = None
    for base in C.OVERPASS_API_BASES:
        try:
            r = requests.post(base + "/interpreter", data={"data": q}, headers=HEADERS, timeout=120)
            r.raise_for_status()
            n_ = [el["tags"].get("total", "?") for el in r.json()["elements"]]
            return f"{n_[0]} supermarkets, {n_[1]} pharmacies, {n_[2]} hospitals in the study area (via {base})"
        except Exception as ex:  # noqa: BLE001
            last = ex
    raise RuntimeError(f"all Overpass servers failed: {last}")


# ------------------------------------------------------------------ ABS
@check("ABS Census DataPack and SA1 boundaries", required=False)
def abs_files():
    out = []
    for url in (C.ABS_GCP_URL, C.ABS_SA1_BOUNDARIES_URL):
        r = requests.head(url, headers=HEADERS, timeout=60, allow_redirects=True)
        r.raise_for_status()
        out.append(f"{url.rsplit('/', 1)[-1]} ok")
    return "; ".join(out)


# ------------------------------------------------------------------ satellite
def _stac_check(provider, required):
    @check(f"Satellite ({provider}): recent scenes and band files", required=required)
    def run():
        from pystac_client import Client
        p = C.SAT_PROVIDERS[provider]
        end = dt.date.today()
        items = list(Client.open(p["stac"]).search(collections=p["collections"], bbox=C.BBOX,
                                                   datetime=f"{end - dt.timedelta(days=30)}/{end}", max_items=20).items())
        if not items:
            raise RuntimeError("no scenes over the study area in the last 30 days")
        latest = max(items, key=lambda i: i.datetime)
        missing = [b for b in p["bands"].values() if b not in latest.assets]
        if missing:
            raise RuntimeError(f"scene {latest.id} lacks bands {missing}; has {sorted(latest.assets)[:20]}")
        from .satellite import _href
        h = requests.head(_href(latest.assets[p["bands"]["red"]].href), timeout=60, allow_redirects=True)
        h.raise_for_status()
        return f"{len(items)} scenes in 30 days; latest {latest.datetime.date()} ({latest.id}); band files readable"
    return run


def main() -> int:
    checks = [vicmap_capabilities, *[_layer_check(l) for l in REQUIRED_FIELDS], vicmap_paging,
              _identity_check(C.LAYER_PARCEL_DETAIL, ("parcel", "mp"), ["spi"], "lot-and-plan numbers"),
              _identity_check(C.LAYER_ADDRESS, ("address",), ["ezi_address", "house_number", "road_name"], "addresses"),
              gtfs, overpass,
              overture, services, abs_files,
              _stac_check("dea", False), _stac_check("earthsearch", False)]
    print("Checking data sources...\n")
    for c in checks:
        c()
        r = results[-1]
        mark = "PASS" if r["ok"] else ("FAIL" if r["required"] else "WARN")
        print(f"[{mark}] {r['check']}\n       {r['detail']}\n")
    status.verify(results)
    bad = [r for r in results if not r["ok"] and r["required"]]
    if bad:
        print(f"{len(bad)} required check(s) failed. Paste this output into the chat to get the code adjusted.")
        return 1
    print("All required sources look good. Next: sh run_pipeline.sh full")
    return 0


if __name__ == "__main__":
    sys.exit(main())
