"""Phase 2 - Live satellite layer (Sentinel-2, 10 m, new pass every few days).

Run from the project folder:
    python -m src.satellite                    # latest imagery up to today
    python -m src.satellite --date 2026-03-01  # imagery as at a past date
    python -m src.satellite --provider earthsearch

What it produces (data/processed/):
    sat_current.tif    cloud-free composite of the most recent clear passes
    sat_baseline.tif   same season, one year earlier (for change detection)
    sat_truecolour.png true-colour image for the map + sat_meta.json (dates, scenes, bounds)

Per-lot measures (computed in src/process.py via parcel_stats):
    ndvi_mean   vegetation greenness (-1..1)
    green_frac  share of the lot that is vegetated (trees, lawn)
    ndbi_mean   built-up / hard-surface index
    d_ndvi, d_ndbi  change versus a year ago -> flags lots already being cleared or built on

Honest limits: Sentinel-2 pixels are 10 m x 10 m, so a typical 600 m2 suburban lot holds
about six pixels. These measures describe the lot as a whole; they cannot see individual
roofs, driveways or trees. That needs sub-metre aerial imagery (see README, Phase 3).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from collections import defaultdict

import numpy as np
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.vrt import WarpedVRT
from rasterio.warp import reproject

from . import config as C
from . import status

os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")

READ_RES = 10.0


# ----------------------------------------------------------------------------- grid helpers
def study_grid(res: float):
    """Target grid in GDA2020 / MGA55 covering config.BBOX."""
    t = Transformer.from_crs(C.CRS_WGS84, C.CRS_METRIC, always_xy=True)
    xs, ys = zip(*[t.transform(x, y) for x in (C.BBOX[0], C.BBOX[2]) for y in (C.BBOX[1], C.BBOX[3])])
    minx, maxx = np.floor(min(xs) / res) * res, np.ceil(max(xs) / res) * res
    miny, maxy = np.floor(min(ys) / res) * res, np.ceil(max(ys) / res) * res
    w, h = int((maxx - minx) / res), int((maxy - miny) / res)
    return Affine(res, 0, minx, 0, -res, maxy), w, h


def _href(asset_href: str) -> str:
    if asset_href.startswith("s3://dea-public-data/"):
        return asset_href.replace("s3://dea-public-data/", "https://data.dea.ga.gov.au/")
    if asset_href.startswith("s3://"):
        return "/vsis3/" + asset_href[5:]
    return asset_href


def _read(href: str, transform, w, h, resampling) -> np.ndarray:
    with rasterio.open(_href(href)) as src:
        with WarpedVRT(src, crs=C.CRS_METRIC, transform=transform, width=w, height=h,
                       resampling=resampling, src_nodata=src.nodata, nodata=0) as vrt:
            return vrt.read(1).astype("float32")


# ----------------------------------------------------------------------------- search
def search_dates(provider: str, start: dt.date, end: dt.date) -> list[tuple[dt.date, list]]:
    """Return [(date, [items...])] with acceptable cloud cover, newest first."""
    from pystac_client import Client

    p = C.SAT_PROVIDERS[provider]
    client = Client.open(p["stac"])
    search = client.search(collections=p["collections"], bbox=C.BBOX,
                           datetime=f"{start.isoformat()}/{end.isoformat()}", max_items=500)
    by_date = defaultdict(list)
    for item in search.items():
        cc = item.properties.get(p["cloud_property"])
        if cc is not None and cc > C.SAT_MAX_CLOUD:
            continue
        by_date[item.datetime.date()].append(item)
    return sorted(by_date.items(), key=lambda kv: kv[0], reverse=True)


# ----------------------------------------------------------------------------- composite
def composite(provider: str, dated_items: list[tuple[dt.date, list]], max_dates: int = 4):
    """Median of clear pixels across up to `max_dates` passes, on a 10 m grid."""
    p = C.SAT_PROVIDERS[provider]
    transform, w, h = study_grid(READ_RES)
    keys = ["red", "green", "blue", "nir", "swir"]
    stacks = {k: [] for k in keys}
    used = []
    for date, items in dated_items[:max_dates]:
        layer = {k: np.full((h, w), np.nan, "float32") for k in keys}
        for item in items:  # several Sentinel-2 tiles may cover the council on one date
            assets = item.assets
            if any(p["bands"][k] not in assets for k in keys + ["mask"]):
                continue
            mask = _read(assets[p["bands"]["mask"]].href, transform, w, h, Resampling.nearest)
            clear = np.isin(mask, p["clear_values"])
            for k in keys:
                arr = _read(assets[p["bands"][k]].href, transform, w, h, Resampling.bilinear) * p["scale"]
                fill = clear & np.isnan(layer[k]) & (arr > 0)
                layer[k][fill] = arr[fill]
        clear_share = float(np.mean(~np.isnan(layer["red"])))
        print(f"    {date}: {len(items)} tile(s), {clear_share:.0%} of area clear")
        if clear_share < 0.05:
            continue
        for k in keys:
            stacks[k].append(layer[k])
        used.append({"date": date.isoformat(), "scenes": [i.id for i in items], "clear_share": round(clear_share, 3)})
    if not used:
        raise RuntimeError("No clear imagery found in the search window. Try a longer window or another provider.")
    import warnings
    with np.errstate(all="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN where every pass was cloudy
        bands = {k: np.nanmedian(np.stack(v), axis=0) for k, v in stacks.items()}
    return bands, transform, used


def indices(b: dict) -> dict:
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (b["nir"] - b["red"]) / (b["nir"] + b["red"])
        ndbi = (b["swir"] - b["nir"]) / (b["swir"] + b["nir"])
    return {"ndvi": ndvi.astype("float32"), "ndbi": ndbi.astype("float32")}


def write_stack(path, layers: dict, transform):
    names = list(layers)
    h, w = layers[names[0]].shape
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=len(names), dtype="float32",
                       crs=C.CRS_METRIC, transform=transform, nodata=np.nan, compress="deflate") as dst:
        for i, n in enumerate(names, 1):
            dst.write(layers[n], i)
            dst.set_band_description(i, n)


def write_truecolour(bands: dict, transform, png_path):
    """Warp RGB to lon/lat and save a stretched PNG + its bounds for the web map."""
    from PIL import Image

    h, w = bands["red"].shape
    lon0, lat0, lon1, lat1 = C.BBOX
    out_w = w
    out_h = int(round(w * (lat1 - lat0) / (lon1 - lon0) / np.cos(np.radians((lat0 + lat1) / 2))))
    dst_t = rasterio.transform.from_bounds(lon0, lat0, lon1, lat1, out_w, out_h)
    rgb = []
    for k in ("red", "green", "blue"):
        dst = np.full((out_h, out_w), np.nan, "float32")
        reproject(bands[k], dst, src_transform=transform, src_crs=C.CRS_METRIC, dst_transform=dst_t,
                  dst_crs=C.CRS_WGS84, resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)
        rgb.append(dst)
    rgb = np.stack(rgb, -1)
    alpha = (~np.isnan(rgb).any(-1)).astype("uint8") * 255
    img = np.clip(np.nan_to_num(rgb) / 0.25, 0, 1) ** (1 / 1.4)  # simple stretch + gamma
    Image.fromarray(np.dstack([(img * 255).astype("uint8"), alpha]), "RGBA").save(png_path)
    return [lon0, lat0, lon1, lat1]


# ----------------------------------------------------------------------------- per-lot stats
def parcel_stats(parcels, current_tif, baseline_tif=None):
    """Area-weighted satellite measures for each parcel (GeoDataFrame in CRS_METRIC)."""
    import pandas as pd

    transform, w, h = study_grid(C.SAT_RES_M)

    def load(path):
        out = {}
        with rasterio.open(path) as src:
            for i, name in enumerate(src.descriptions, 1):
                if name in ("ndvi", "ndbi"):
                    dst = np.full((h, w), np.nan, "float32")
                    reproject(src.read(i), dst, src_transform=src.transform, src_crs=src.crs,
                              dst_transform=transform, dst_crs=C.CRS_METRIC, resampling=Resampling.bilinear,
                              src_nodata=np.nan, dst_nodata=np.nan)
                    out[name] = dst
        return out

    cur = load(current_tif)
    base = load(baseline_tif) if baseline_tif and os.path.exists(baseline_tif) else None

    labels = rasterize(((g, i + 1) for i, g in enumerate(parcels.geometry.values)),
                       out_shape=(h, w), transform=transform, fill=0, dtype="int32")
    n = len(parcels) + 1

    def mean(arr, extra_mask=None):
        ok = (labels > 0) & ~np.isnan(arr)
        if extra_mask is not None:
            ok &= extra_mask
        s = np.bincount(labels[ok], weights=arr[ok], minlength=n)
        c = np.bincount(labels[ok], minlength=n)
        with np.errstate(invalid="ignore", divide="ignore"):
            return (s / c)[1:], c[1:]

    ndvi, count = mean(cur["ndvi"])
    ndbi, _ = mean(cur["ndbi"])
    green, _ = mean((cur["ndvi"] > C.NDVI_GREEN).astype("float32"), ~np.isnan(cur["ndvi"]))
    df = pd.DataFrame({"ndvi_mean": ndvi, "green_frac": green, "ndbi_mean": ndbi,
                       "sat_pixels": count}, index=parcels.index)
    if base is not None:
        b_ndvi, _ = mean(base["ndvi"])
        b_ndbi, _ = mean(base["ndbi"])
        df["d_ndvi"] = df["ndvi_mean"] - b_ndvi
        df["d_ndbi"] = df["ndbi_mean"] - b_ndbi
        df["recent_change"] = (df["d_ndvi"] < -C.CHANGE_NDVI_DROP) & (df["d_ndbi"] > C.CHANGE_NDBI_RISE)
    else:
        df["d_ndvi"] = df["d_ndbi"] = np.nan
        df["recent_change"] = False
    # Fewer than ~3 native pixels' worth of samples -> low confidence
    df["sat_low_confidence"] = df["sat_pixels"] < 3 * (READ_RES / C.SAT_RES_M) ** 2
    return df.round(3)


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="'as at' date (YYYY-MM-DD)")
    ap.add_argument("--provider", default="dea", choices=list(C.SAT_PROVIDERS))
    a = ap.parse_args()

    C.PROCESSED.mkdir(parents=True, exist_ok=True)
    end = dt.date.fromisoformat(a.date)
    start = end - dt.timedelta(days=C.SAT_LOOKBACK_DAYS)
    b_end = end.replace(year=end.year - C.SAT_BASELINE_YEARS)
    b_start = b_end - dt.timedelta(days=C.SAT_LOOKBACK_DAYS)

    print(f"Current imagery: {start} to {end} ({a.provider})")
    cur_items = search_dates(a.provider, start, end)
    if not cur_items:
        raise SystemExit("No scenes found. Try --provider earthsearch or a later --date.")
    cur_bands, transform, cur_used = composite(a.provider, cur_items)
    write_stack(C.PROCESSED / "sat_current.tif", {**indices(cur_bands), **cur_bands}, transform)
    bounds = write_truecolour(cur_bands, transform, C.PROCESSED / "sat_truecolour.png")

    print(f"Baseline imagery: {b_start} to {b_end}")
    base_used = []
    base_items = search_dates(a.provider, b_start, b_end)
    if base_items:
        base_bands, _, base_used = composite(a.provider, base_items)
        write_stack(C.PROCESSED / "sat_baseline.tif", {**indices(base_bands), **base_bands}, transform)
    else:
        print("  no baseline scenes - change detection will be skipped")

    meta = {"provider": a.provider, "as_at": a.date, "current": cur_used, "baseline": base_used,
            "png_bounds": bounds, "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "attribution": ("Contains modified Copernicus Sentinel data, processed by Geoscience Australia "
                            "(Digital Earth Australia), CC BY 4.0" if a.provider == "dea"
                            else "Contains modified Copernicus Sentinel data (Element 84 Earth Search)")}
    (C.PROCESSED / "sat_meta.json").write_text(json.dumps(meta, indent=2))
    note = "" if base_used else "No baseline imagery from a year earlier, so change detection is off."
    status.source("satellite", label=f"Satellite imagery (Sentinel-2, {a.provider})",
                  url=C.SAT_PROVIDERS[a.provider]["stac"], rows=len(cur_used),
                  state="ok" if base_used else "warning", note=note,
                  updates="new pass every few days, cloud permitting")
    print(f"Done. Latest pass used: {cur_used[0]['date']}. Next: python -m src.process")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        status.source("satellite", label="Satellite imagery (Sentinel-2)", state="warning" if
                      (C.PROCESSED / "sat_current.tif").exists() else "failed",
                      note=f"Refresh failed: {e}")
        raise
