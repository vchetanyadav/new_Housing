"""Lot-and-plan numbers (SPI, e.g. 12\\LP12345) and street addresses for each lot.

Planners know lots by their Standard Parcel Identifier and street address, not by Vicmap's
internal IDs. Both come from Vicmap open data:
  * parcel detail (v_parcel_mp)  -> SPI, lot number, plan number
  * address points (Vicmap Address) -> street address
Column names are detected rather than assumed, and what was found is printed, so a schema
change shows up clearly instead of silently producing blanks.
"""
from __future__ import annotations

import re

import geopandas as gpd
import numpy as np
import pandas as pd

from . import config as C

DETAIL = C.RAW / "parcel_detail.parquet"
ADDRESSES = C.RAW / "addresses.parquet"


def _col(df, *candidates, contains: tuple[str, ...] = ()) -> str | None:
    cols = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c in cols:
            return cols[c]
    for c in df.columns:
        lc = c.lower()
        if contains and all(k in lc for k in contains):
            return c
    return None


def _clean(v) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("none", "nan", "null") else s


# ----------------------------------------------------------------------------- SPI
def lot_plan_label(spi: str) -> str:
    """'12\\LP12345' -> 'Lot 12 on LP12345'; other formats are returned unchanged."""
    m = re.fullmatch(r"\s*([^\\]+)\\([A-Z]{1,3}\d+[A-Z]?)\s*", spi or "")
    if not m:
        return spi or ""
    lot, plan = m.groups()
    if lot.upper().startswith("CM"):
        return f"Common property {lot} on {plan}"
    return f"Lot {lot} on {plan}"


def _norm_ids(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)


def attach_spi(parcels: gpd.GeoDataFrame, rep: gpd.GeoSeries, own_keys) -> tuple[pd.DataFrame, str]:
    """SPI for each lot. Tries every pairing of the lot layer's ID columns with the detail table's ID
    columns and uses whichever matches best; falls back to location if no pairing matches well."""
    out = pd.DataFrame({"spi": "", "lot_plan": ""}, index=parcels.index)
    if not DETAIL.exists():
        return out, "unavailable"
    try:
        d = gpd.read_parquet(DETAIL)              # SPI with outlines (matched by location)
    except (ValueError, TypeError):
        d = pd.read_parquet(DETAIL)               # SPI with parcel IDs only (matched by ID)
    d.columns = [c.lower() if c != "geometry" else c for c in d.columns]
    spi_col = _col(d, "parcel_spi", "spi", contains=("spi",))
    if spi_col is None:
        print(f"  [WARNING] parcel detail has no SPI column. Columns: {sorted(d.columns)}")
        return out, "no SPI column"
    print(f"  parcel detail: SPI from '{spi_col}'")
    d = d[d[spi_col].map(_clean) != ""]

    if isinstance(own_keys, pd.Series):
        own_keys = {"parcel_id": own_keys}
    own_keys = {k: v for k, v in (own_keys or {}).items() if v is not None}
    detail_ids = [c for c in d.columns if c not in (spi_col, "geometry") and ("pfi" in c or "ufi" in c)]
    best, best_rate, rates = None, 0.0, []
    for dc in detail_ids:
        dvals = set(_norm_ids(d[dc]))
        for oc, ov in own_keys.items():
            rate = float(_norm_ids(ov).isin(dvals).mean())
            rates.append(f"{oc}->{dc} {rate:.0%}")
            if rate > best_rate:
                best, best_rate = (oc, dc), rate
    if rates:
        print("  ID match rates: " + ", ".join(rates))

    method = "location"
    key_col = best[1] if best else None
    if best and best_rate > 0.8:
        oc, dc = best
        dd = d.assign(_k=_norm_ids(d[dc])).drop_duplicates("_k").set_index("_k")[spi_col]
        out["spi"] = _norm_ids(own_keys[oc]).map(dd).map(_clean).fillna("")
        method = f"shared ID ({oc} = {dc})"
        own_key = own_keys[oc]
    else:
        own_key = next(iter(own_keys.values()), None)
    if method == "location":
        if "geometry" not in d.columns:
            print(f"  [WARNING] the best ID pairing matched only {best_rate:.0%} of lots and the detail table has "
                  "no outlines to match by location. Run: sh run_pipeline.sh lotplan")
            return out, "IDs did not match"
        d = d.set_geometry("geometry").to_crs(C.CRS_METRIC)
        pts = gpd.GeoDataFrame(geometry=list(rep.values), index=parcels.index, crs=C.CRS_METRIC)
        j = gpd.sjoin(pts, d[[spi_col, "geometry"]], predicate="within", how="left")
        j = j[~j.index.duplicated()]
        out["spi"] = j[spi_col].map(_clean).reindex(parcels.index).fillna("")
    out["lot_plan"] = out["spi"].map(lot_plan_label)
    return out, method


# ----------------------------------------------------------------------------- addresses
def _format_address(row: pd.Series, cols: dict) -> str:
    if cols.get("full"):
        raw = _clean(row[cols["full"]])
        if raw:
            return _tidy(raw)
    parts = []
    unit = _clean(row[cols["unit"]]) if cols.get("unit") else ""
    num = _clean(row[cols["num"]]) if cols.get("num") else ""
    num2 = _clean(row[cols["num2"]]) if cols.get("num2") else ""
    if num and num2 and num2 != "0":
        num = f"{num}-{num2}"
    if unit:
        num = f"{unit}/{num}" if num else unit
    street = " ".join(x for x in (_clean(row[cols["road"]]) if cols.get("road") else "",
                                  _clean(row[cols["road_type"]]) if cols.get("road_type") else "") if x)
    locality = _clean(row[cols["locality"]]) if cols.get("locality") else ""
    pc = _clean(row[cols["postcode"]]) if cols.get("postcode") else ""
    first = " ".join(x for x in (num, street) if x)
    parts = [p for p in (first, " ".join(x for x in (locality, pc) if x)) if p]
    return _tidy(", ".join(parts))


def _tidy(s: str) -> str:
    """'12 SMITH STREET COBURG 3058' -> '12 Smith Street Coburg 3058' (keeps unit/number tokens)."""
    words = []
    for w in s.split():
        words.append(w if any(ch.isdigit() for ch in w) else w.capitalize())
    return " ".join(words).replace(" ,", ",")


def _sort_number(a: str) -> tuple:
    m = re.match(r"(?:(\d+)/)?(\d+)", a)
    return (int(m.group(2)) if m else 10**9, int(m.group(1)) if m and m.group(1) else 0, a)


def attach_addresses(parcels: gpd.GeoDataFrame) -> tuple[pd.DataFrame, str]:
    """Main street address for each lot plus how many other addresses sit on it."""
    out = pd.DataFrame({"address": "", "address_count": 0}, index=parcels.index)
    if not ADDRESSES.exists():
        return out, "unavailable"
    a = gpd.read_parquet(ADDRESSES)
    a.columns = [c.lower() if c != "geometry" else c for c in a.columns]
    cols = {
        "full": _col(a, "ezi_address", "full_address", "address", contains=("ezi", "address")),
        "unit": _col(a, "blg_unit_id_1", "unit_number", contains=("unit", "id_1")),
        "num": _col(a, "house_number_1", "house_number", contains=("house_number_1",)),
        "num2": _col(a, "house_number_2"),
        "road": _col(a, "road_name", "street_name"),
        "road_type": _col(a, "road_type", "street_type"),
        "locality": _col(a, "locality_name", "locality", "suburb"),
        "postcode": _col(a, "postcode", "post_code"),
    }
    found = {k: v for k, v in cols.items() if v}
    print(f"  addresses: using columns {found}")
    if not cols["full"] and not cols["road"]:
        print(f"  [WARNING] address layer has no usable address columns. Columns: {sorted(a.columns)}")
        return out, "no address columns"
    a = a.set_geometry("geometry").to_crs(C.CRS_METRIC)
    a = a[a.geometry.notna()]
    a["addr"] = a.apply(lambda r: _format_address(r, cols), axis=1)
    a = a[a["addr"] != ""]
    j = gpd.sjoin(a[["addr", "geometry"]], parcels[["geometry"]], predicate="within", how="inner")
    if j.empty:
        return out, "no addresses inside lots"
    grouped = j.groupby("index_right")["addr"].agg(lambda s: sorted(set(s), key=_sort_number))
    out["address"] = grouped.map(lambda lst: lst[0]).reindex(parcels.index).fillna("")
    out["address_count"] = grouped.map(len).reindex(parcels.index).fillna(0).astype(int)
    return out, "location (address point inside lot)"
