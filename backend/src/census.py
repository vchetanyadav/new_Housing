"""Phase 2 - ABS 2021 Census demand indicators by SA1 (small areas of ~400 people).

Uses the General Community Profile table G02 "Selected medians and averages":
    Median_rent_weekly, Median_tot_hhd_inc_weekly, Average_household_size, Median_age_persons

Indicator `rent_to_income` = median weekly rent / median weekly household income.
This is a PROXY for rental pressure, not the official "rental stress" measure (which
needs household-level data). Values above ~0.30 indicate areas where typical rents take
more than 30% of typical household income.
"""
from __future__ import annotations

import io
import zipfile

import geopandas as gpd
import pandas as pd
import requests

from . import config as C

OUT = C.RAW / "sa1_census.parquet"
G02_COLS = ["Median_rent_weekly", "Median_tot_hhd_inc_weekly", "Average_household_size", "Median_age_persons"]


def _get(url: str, path):
    if not path.exists():
        print(f"  downloading {path.name} ...")
        with requests.get(url, stream=True, timeout=600, headers={"User-Agent": "smart-housing-optimiser"}) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
    return path


def fetch_census() -> None:
    if OUT.exists():
        print(f"  [skip] {OUT.name} already built")
        return
    shp_zip = _get(C.ABS_SA1_BOUNDARIES_URL, C.RAW / "SA1_2021_AUST_SHP_GDA2020.zip")
    gcp_zip = _get(C.ABS_GCP_URL, C.RAW / "2021_GCP_SA1_for_VIC_short-header.zip")

    sa1 = gpd.read_file(f"zip://{shp_zip}", bbox=C.BBOX)
    sa1 = sa1[["SA1_CODE21", "geometry"]].to_crs(C.CRS_METRIC)

    with zipfile.ZipFile(gcp_zip) as z:
        name = next(n for n in z.namelist() if "G02" in n and n.lower().endswith("sa1.csv"))
        g02 = pd.read_csv(io.BytesIO(z.read(name)), dtype={"SA1_CODE_2021": str})
    missing = [c for c in G02_COLS if c not in g02.columns]
    if missing:
        raise SystemExit(f"G02 is missing {missing}. Columns found: {list(g02.columns)}")
    df = sa1.merge(g02[["SA1_CODE_2021"] + G02_COLS], left_on="SA1_CODE21", right_on="SA1_CODE_2021", how="left")
    df.drop(columns="SA1_CODE_2021").to_parquet(OUT)
    print(f"  [ok] {OUT.name}: {len(df):,} SA1s")


def attach_census(parcels: gpd.GeoDataFrame, rep_points: gpd.GeoSeries) -> pd.DataFrame:
    """SA1 code and G02 indicators for each parcel (by its representative point)."""
    if not OUT.exists():
        print("  (no census file - skipping demand indicators)")
        return pd.DataFrame(index=parcels.index)
    sa1 = gpd.read_parquet(OUT).to_crs(C.CRS_METRIC)
    pts = gpd.GeoDataFrame(geometry=rep_points.values, index=parcels.index, crs=C.CRS_METRIC)
    j = gpd.sjoin(pts, sa1, predicate="within", how="left")
    j = j[~j.index.duplicated()]
    out = j[["SA1_CODE21"] + G02_COLS].rename(columns={
        "SA1_CODE21": "sa1", "Median_rent_weekly": "median_rent_wk",
        "Median_tot_hhd_inc_weekly": "median_hh_income_wk", "Average_household_size": "avg_hh_size",
        "Median_age_persons": "median_age"})
    inc = out["median_hh_income_wk"].where(out["median_hh_income_wk"] > 0)
    out["rent_to_income"] = (out["median_rent_wk"] / inc).round(3)
    return out
