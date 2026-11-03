"""Transparent suitability scoring and dwelling-capacity scenarios.

Every number is explainable: a lot's score is a weighted average of 0-1 factors, the weights
are shown in the app, every excluded lot carries the first rule it failed, and anything the
result depends on but couldn't be measured is returned as a warning rather than papered over.

Capacity is THEORETICAL: lot area x target density, minus the existing dwelling. It ignores
ownership, consolidation, market feasibility, setbacks, access and covenants.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config as C


@dataclass
class Scenario:
    radius_m: float = C.DEFAULT_RADIUS_M
    min_lot_m2: float = C.DEFAULT_MIN_LOT_M2
    min_width_m: float = C.DEFAULT_MIN_WIDTH_M
    max_coverage: float = C.DEFAULT_MAX_COVERAGE
    dwellings_per_ha: float = C.DEFAULT_DWELLINGS_PER_HA
    zones: tuple[str, ...] = C.CANDIDATE_ZONE_PREFIXES
    exclude_overlays: bool = True          # heritage, flood, contamination, acquisition, bushfire
    exclude_review_overlays: bool = False  # character, design, vegetation, landscape ...
    use_walking: bool = True
    exclude_recent_change: bool = True
    max_green_frac: float = 1.0
    services_walk_m: float = C.DEFAULT_SERVICES_WALK_M   # "within reach" for everyday needs
    max_supermarket_m: float = 0.0                       # 0 = off; else require a supermarket this close
    w_proximity: float = 0.30
    w_lot_size: float = 0.20
    w_open_land: float = 0.20
    w_demand: float = 0.15
    w_services: float = 0.15


def _has(df, col):
    return col in df and df[col].notna().any()


def score(parcels: pd.DataFrame, s: Scenario) -> pd.DataFrame:
    df = parcels.copy()
    zone = df["zone"].fillna("")
    warnings: list[str] = []

    # ---- distance: walking if requested and available; never silently substituted
    if s.use_walking and _has(df, "walk_m"):
        dist = df["walk_m"]
        method = "walking network"
    elif s.use_walking:
        dist = df["dist_station_m"]
        method = "straight line (walking network unavailable)"
        warnings.append("Walking distances are unavailable, so straight-line distance was used. "
                        "This overstates access where rail lines, creeks or main roads block a direct walk.")
    else:
        dist = df["dist_station_m"]
        method = "straight line"
    df["access_m"] = dist
    df.attrs["access_method"] = method

    # ---- 0-1 factors
    df["f_proximity"] = (1 - dist / s.radius_m).clip(0, 1).fillna(0)
    df["f_lot_size"] = ((df["area_m2"] - s.min_lot_m2) / 1000).clip(0, 1)
    df["f_open_land"] = (1 - df["coverage"]).clip(0, 1)
    if _has(df, "rent_to_income"):
        df["f_demand"] = ((df["rent_to_income"] - 0.15) / 0.20).clip(0, 1).fillna(0.5)
    else:
        df["f_demand"] = 0.5
        if s.w_demand > 0:
            warnings.append("Census data is unavailable, so rental pressure is set to neutral for every lot.")
    daily = [k for k, v in C.AMENITIES.items() if v["kind"] == "daily" and f"{k}_m" in df]
    if daily and any(_has(df, f"{k}_m") for k in daily):
        met = np.column_stack([(df[f"{k}_m"] <= s.services_walk_m).to_numpy() for k in daily])
        df["services_met"] = met.sum(axis=1)
        df["f_services"] = met.mean(axis=1)
    else:
        df["services_met"] = np.nan
        df["f_services"] = 0.5
        if s.w_services > 0:
            warnings.append("Services data is unavailable, so access to everyday needs is set to neutral for every lot.")
    w = np.array([s.w_proximity, s.w_lot_size, s.w_open_land, s.w_demand, s.w_services], dtype=float)
    w = w / w.sum() if w.sum() > 0 else np.ones(5) / 5
    df["score"] = (df[["f_proximity", "f_lot_size", "f_open_land", "f_demand", "f_services"]].to_numpy() @ w).round(3)

    # ---- eligibility rules; the first failing rule is recorded
    r = pd.Series("", index=df.index)

    def rule(mask, reason):
        r[(r == "") & pd.Series(mask, index=df.index).fillna(False).astype(bool)] = reason

    rule(~zone.str.startswith(tuple(s.zones)), "not a candidate zone")
    if method == "walking network":
        rule(dist.isna(), "no walking route to a station found")
    rule(dist > s.radius_m, "too far from station")
    rule(df["area_m2"] < s.min_lot_m2, "lot too small")
    if "lot_width_m" in df:
        rule(df["lot_width_m"] < s.min_width_m, "lot too narrow")
    rule(df["coverage"] > s.max_coverage, "already well built-out")
    if s.max_supermarket_m > 0 and "supermarket_m" in df:
        rule(~(df["supermarket_m"] <= s.max_supermarket_m), "no supermarket within reach")
    rule(df["stacked"], "existing units / strata")
    exc_col = "overlays_exclude" if "overlays_exclude" in df else "overlays"
    if s.exclude_overlays:
        rule(df[exc_col].fillna("") != "", "heritage, flood or other excluding overlay")
    if s.exclude_review_overlays and "overlays_review" in df:
        rule(df["overlays_review"].fillna("") != "", "overlay needing review")
    if s.exclude_recent_change and "recent_change" in df:
        rule(df["recent_change"], "satellite: recent clearing / construction")
    if s.max_green_frac < 1.0 and "green_frac" in df:
        rule(df["green_frac"] > s.max_green_frac, "satellite: heavily vegetated")
    df["excluded_reason"] = r
    df["eligible"] = r == ""
    df["needs_review"] = df["eligible"] & (df["overlays_review"].fillna("") != "") if "overlays_review" in df else False

    if s.exclude_recent_change and not _has(df, "recent_change"):
        warnings.append("Satellite change detection is unavailable, so lots already being redeveloped can't be screened out.")

    # ---- theoretical capacity
    capacity = np.floor(df["area_m2"] * s.dwellings_per_ha / 10_000)
    df["new_dwellings"] = np.where(df["eligible"], (capacity - 1).clip(lower=0), 0).astype(int)
    df.loc[~df["eligible"], "score"] = 0.0
    df["station"] = df["walk_station"].fillna(df["nearest_station"]) if "walk_station" in df else df["nearest_station"]
    df.attrs["warnings"] = warnings
    df.attrs["services_walk_m"] = s.services_walk_m
    return df


def summarise(df: pd.DataFrame) -> dict:
    e = df[df["eligible"]]
    return {
        "eligible_lots": int(len(e)),
        "new_dwellings": int(e["new_dwellings"].sum()),
        "needs_review": int(e["needs_review"].sum()) if "needs_review" in e else 0,
        "services_access": {k: float((e[f"{k}_m"] <= df.attrs.get("services_walk_m", C.DEFAULT_SERVICES_WALK_M)).mean())
                            for k, v in C.AMENITIES.items() if v["kind"] == "daily" and f"{k}_m" in e and len(e)},
        "land_ha": float(e["area_m2"].sum() / 10_000),
        "open_land_ha": float(e["open_m2"].sum() / 10_000),
        "by_station": (e.groupby("station")
                        .agg(lots=("parcel_id", "size"), new_dwellings=("new_dwellings", "sum"))
                        .sort_values("new_dwellings", ascending=False)),
        "exclusions": df.loc[~df["eligible"], "excluded_reason"].value_counts(),
    }
