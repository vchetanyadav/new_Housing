"""Step 2 - Turn raw layers into one table of parcels with planning features.

Run from the project folder:  python -m src.process

For every land parcel in the council it works out:
  zone, lot area and width, building footprint area and site coverage, walking and
  straight-line distance to the nearest station, every planning overlay covering the lot
  (and whether it excludes the lot or needs review), Census indicators and satellite measures.
Problems found along the way are recorded in status.json and shown in the app.
The result (data/processed/parcels.parquet) is what the app scores.
"""
from __future__ import annotations

import sys

import geopandas as gpd
import numpy as np
import pandas as pd
import re

import shapely

from . import config as C
from . import status


def _find_col(df: pd.DataFrame, candidates: list[str], contains: str | None = None) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    if contains:
        for c in df.columns:
            if contains in c and df[c].dtype == object:
                return c
    return None


def load(name: str) -> gpd.GeoDataFrame:
    path = C.RAW / f"{name}.parquet"
    if not path.exists():
        sys.exit(f"Missing {path}. Run: python -m src.ingest")
    gdf = gpd.read_parquet(path)
    gdf.columns = [c.lower() if c != "geometry" else c for c in gdf.columns]
    return gdf.set_geometry("geometry").to_crs(C.CRS_METRIC)


def council_zones(zones: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, str]:
    lga_col = _find_col(zones, ["lga", "lga_name", "lga_name_full"], contains="lga")
    if lga_col is None:
        sys.exit(f"No LGA column in zones layer. Columns: {list(zones.columns)}")
    names = zones[lga_col].astype(str).str.upper()
    mask = names.isin(C.LGA_NAMES) | names.str.contains("MERRI|MORELAND", regex=True)
    if not mask.any():
        sys.exit(f"No zones matched {C.LGA_NAMES}. Values seen: {sorted(names.unique())[:20]}")
    zone_col = _find_col(zones, ["zone_code", "zone_code_num", "zonecode"], contains="zone_code")
    if zone_col is None:
        sys.exit(f"No zone code column. Columns: {list(zones.columns)}")
    z = zones.loc[mask, [zone_col, "geometry"]].rename(columns={zone_col: "zone"})
    z["zone"] = z["zone"].astype(str).str.upper()
    return z.reset_index(drop=True), lga_col


def overlay_type(code: str) -> str:
    """'HO123' -> 'HO', 'DDO5' -> 'DDO', 'LSIO' -> 'LSIO'."""
    m = re.match(r"[A-Z]+", str(code).upper().strip())
    return m.group(0) if m else str(code).upper()


def overlay_category(code: str) -> str:
    t = overlay_type(code)
    if t in C.OVERLAYS_EXCLUDE:
        return "exclude"
    if t in C.OVERLAYS_IGNORE:
        return "ignore"
    return "review"   # listed review overlays AND anything unrecognised


def overlay_assessment(parcels: gpd.GeoDataFrame, overlays: gpd.GeoDataFrame, code_col: str) -> pd.DataFrame:
    """For every lot, the overlays covering at least OVERLAY_MIN_SHARE of its area."""
    ov = overlays[[code_col, "geometry"]].rename(columns={code_col: "code"})
    ov = ov[ov.geometry.notna()].copy()
    ov["code"] = ov["code"].astype(str).str.upper().str.strip()
    ov["cat"] = ov["code"].map(overlay_category)
    ov = ov[ov["cat"] != "ignore"]
    empty = pd.DataFrame({"overlays": "", "overlays_exclude": "", "overlays_review": ""}, index=parcels.index)
    if ov.empty:
        return empty
    j = gpd.sjoin(parcels[["geometry"]], ov, predicate="intersects", how="inner")
    if j.empty:
        return empty
    inter = shapely.intersection(parcels.geometry.loc[j.index].values, ov.geometry.loc[j["index_right"]].values)
    j["share"] = shapely.area(inter) / parcels.geometry.loc[j.index].area.values
    j = j[j["share"] >= C.OVERLAY_MIN_SHARE]
    # One row per lot+code with the largest share, formatted like "HO123 (100%)"
    j = j.reset_index().groupby(["index", "code", "cat"], as_index=False)["share"].max()
    j["txt"] = j["code"] + " (" + (j["share"] * 100).round().astype(int).astype(str) + "%)"
    agg = lambda s: ", ".join(sorted(s))  # noqa: E731
    out = empty.copy()
    out["overlays"] = j.groupby("index")["txt"].agg(agg).reindex(parcels.index).fillna("")
    for cat in ("exclude", "review"):
        out[f"overlays_{cat}"] = j[j["cat"] == cat].groupby("index")["txt"].agg(agg).reindex(parcels.index).fillna("")
    return out


def main() -> None:
    C.PROCESSED.mkdir(parents=True, exist_ok=True)

    print("Loading layers...")
    zones, _ = council_zones(load("zones"))
    parcels = load("parcels")
    overlays = load("overlays")
    stations = load("stations")
    buildings = load("buildings")

    boundary = shapely.union_all(zones.geometry.buffer(0.5).values)

    # --- Parcels inside the council ----------------------------------------
    pid = _find_col(parcels, ["parcel_spi", "spi", "parcel_pfi", "pfi", "ufi"])
    id_cols = [c for c in parcels.columns if c != "geometry" and ("pfi" in c or "ufi" in c or "spi" in c)]
    keep_cols = list(dict.fromkeys(([pid] if pid else []) + id_cols)) + ["geometry"]
    parcels = parcels[keep_cols].copy()
    parcels["parcel_id"] = parcels[pid].astype(str) if pid else parcels.index.astype(str)
    own_ids = [c for c in id_cols]          # used to match lot-and-plan numbers later
    parcels = parcels[parcels.geometry.notna() & parcels.is_valid]
    parcels = parcels.drop_duplicates(subset="geometry")
    parcels["rep"] = parcels.representative_point()
    parcels = parcels[parcels["rep"].within(boundary)]

    parcels["area_m2"] = parcels.area
    compact = 4 * np.pi * parcels.area / parcels.length.clip(lower=1) ** 2
    parcels = parcels[(parcels["area_m2"].between(100, 20000)) & (compact > 0.15)]  # drop slivers & roads
    # Lot width = short side of the smallest rotated rectangle around the lot (approximates frontage)
    rect = shapely.oriented_envelope(parcels.geometry.values)
    coords = [np.asarray(r.exterior.coords) for r in rect]
    sides = np.array([[np.hypot(*(c[1] - c[0])), np.hypot(*(c[2] - c[1]))] for c in coords])
    parcels["lot_width_m"] = sides.min(axis=1).round(1)
    print(f"  {len(parcels):,} parcels in council")

    # --- Zone of each parcel ------------------------------------------------
    pts = parcels.set_geometry("rep")[["parcel_id", "rep"]]
    j = gpd.sjoin(pts, zones, predicate="within", how="left").drop_duplicates("parcel_id")
    parcels = parcels.merge(j[["parcel_id", "zone"]], on="parcel_id", how="left")

    # --- Planning overlays (all of them, by share of lot covered) ------------
    ov_col = _find_col(overlays, ["zone_code", "overlay_code"], contains="code")
    if ov_col is None:
        sys.exit(f"No overlay code column. Columns: {list(overlays.columns)}")
    parcels = gpd.GeoDataFrame(parcels, geometry="geometry", crs=C.CRS_METRIC)
    parcels = parcels.join(overlay_assessment(parcels, overlays, ov_col))
    print(f"  overlays: {(parcels['overlays_exclude'] != '').sum():,} lots with an excluding overlay, "
          f"{(parcels['overlays_review'] != '').sum():,} with overlays needing review")

    # --- Strata / multi-unit lots (several parcels stacked on one site) ----
    stack = gpd.sjoin(pts, parcels.set_geometry("geometry")[["geometry"]], predicate="within")
    stacked = stack.groupby("parcel_id").size()
    parcels["stacked"] = parcels["parcel_id"].map(stacked).fillna(1) > 1

    # --- Building coverage --------------------------------------------------
    parcels = gpd.GeoDataFrame(parcels, geometry="geometry", crs=C.CRS_METRIC)
    bj = gpd.sjoin(buildings[["geometry"]], parcels[["parcel_id", "geometry"]], predicate="intersects")
    pgeom = parcels.set_index("parcel_id").geometry
    inter = shapely.intersection(bj.geometry.values, pgeom.loc[bj["parcel_id"]].values)
    bj["built"] = shapely.area(inter)
    built = bj.groupby("parcel_id")["built"].sum()
    parcels["built_m2"] = parcels["parcel_id"].map(built).fillna(0.0)
    parcels["coverage"] = (parcels["built_m2"] / parcels["area_m2"]).clip(0, 1)
    parcels["open_m2"] = parcels["area_m2"] - parcels["built_m2"]

    # Alignment check: inside the council, almost every building should sit within some lot.
    # A low share means the two layers don't line up (coordinate problem) or footprints are odd.
    b_in = buildings[buildings.representative_point().within(boundary)]
    b_pts = gpd.GeoDataFrame(geometry=b_in.representative_point().values, crs=C.CRS_METRIC)
    hit = gpd.sjoin(b_pts, parcels[["geometry"]], predicate="within", how="inner")
    align_share = hit.index.nunique() / max(len(b_pts), 1)
    print(f"  buildings: {len(b_in):,} inside the council, {align_share:.0%} of them sit within a lot")

    # --- Distance to nearest train station ---------------------------------
    near = gpd.sjoin_nearest(
        parcels.set_geometry("rep")[["parcel_id", "rep"]],
        stations[["stop_name", "geometry"]], distance_col="dist_station_m",
    ).drop_duplicates("parcel_id")
    parcels = parcels.merge(near[["parcel_id", "stop_name", "dist_station_m"]], on="parcel_id", how="left")
    parcels = parcels.rename(columns={"stop_name": "nearest_station"})

    parcels = gpd.GeoDataFrame(parcels, geometry="geometry", crs=C.CRS_METRIC)

    # --- Walking-network distance ---------------------------------------------
    # If the network can't be built, walking distance is recorded as UNAVAILABLE (not replaced).
    # The app then warns that only straight-line distance exists and labels results accordingly.
    net = None
    try:
        from .accessibility import WalkNetwork, load_walk_graph, walk_distances
        print("Walking network...")
        G = load_walk_graph(boundary)
        net = WalkNetwork(G)
        wd = walk_distances(G, stations, parcels["rep"], net=net)
        parcels = parcels.join(wd)
        parcels["walk_status"] = np.where(parcels["walk_m"].notna(), "ok", f"no walking route within {C.WALK_MAX_M} m")
        n_ok = int(parcels["walk_m"].notna().sum())
        far = int((parcels["walk_m"].isna() & (parcels["dist_station_m"] > C.WALK_MAX_M * 0.75)).sum())
        print(f"  walking distance found for {n_ok:,} of {len(parcels):,} parcels "
              f"({far:,} of the rest are simply more than ~{C.WALK_MAX_M:,} m from any station)")
        # Judge the network only on lots close enough that a walking route should exist.
        near = parcels["dist_station_m"] <= C.WALK_MAX_M * 0.75
        near_ok = float(parcels.loc[near, "walk_m"].notna().mean()) if near.any() else 1.0
        warnings_ = [] if near_ok > 0.95 else [
            f"Only {near_ok:.0%} of lots within {C.WALK_MAX_M * 0.75:,.0f} m of a station have a walking route; "
            "the walking network may be incomplete."]
        status.source("walk_network", label="Walking network (OpenStreetMap)", rows=net.n_edges,
                      state="ok" if not warnings_ else "warning", note=" ".join(warnings_),
                      updates="continuously edited by volunteers",
                      retrieved_at=status.load()["sources"].get("walk_network", {}).get("retrieved_at"))
    except Exception as e:  # noqa: BLE001
        print(f"  [WARNING] walking network unavailable ({e}). Walking distances are marked unavailable.")
        parcels["walk_m"] = np.nan
        parcels["walk_station"] = None
        parcels["walk_min"] = np.nan
        parcels["walk_status"] = "walking network unavailable"
        status.source("walk_network", label="Walking network (OpenStreetMap)", state="failed",
                      note=f"Could not build the walking network: {e}. Only straight-line distances exist.")

    # --- Everyday needs and emergency services ------------------------------------
    from .amenities import attach_services
    print("Everyday needs and emergency services...")
    svc, svc_method = attach_services(parcels["rep"], net)
    parcels = parcels.join(svc)
    parcels["services_method"] = svc_method
    if len(svc.columns):
        daily = [k for k, v in C.AMENITIES.items() if v["kind"] == "daily"]
        within = {k: float((parcels[f"{k}_m"] <= C.DEFAULT_SERVICES_WALK_M).mean()) for k in daily}
        print("  share of lots within " + f"{C.DEFAULT_SERVICES_WALK_M} m ({svc_method}): " +
              ", ".join(f"{C.AMENITIES[k]['label']} {v:.0%}" for k, v in within.items()))

    # --- ABS Census (Phase 2) ------------------------------------------------
    from .census import attach_census
    print("Census...")
    parcels = parcels.join(attach_census(parcels, parcels["rep"]))

    # --- Satellite (Phase 2) -------------------------------------------------
    cur = C.PROCESSED / "sat_current.tif"
    if cur.exists():
        from .satellite import parcel_stats
        print("Satellite measures per lot...")
        parcels = parcels.join(parcel_stats(parcels, cur, C.PROCESSED / "sat_baseline.tif"))
        print(f"  recent clearing/construction flagged on {int(parcels['recent_change'].sum()):,} lots")
    else:
        print("  (no satellite composite yet - run python -m src.satellite)")

    # --- Lot-and-plan numbers and street addresses ---------------------------
    from .identity import attach_addresses, attach_spi
    print("Lot-and-plan numbers and addresses...")
    parcels = gpd.GeoDataFrame(parcels, geometry="geometry", crs=C.CRS_METRIC)
    spi, spi_method = attach_spi(parcels, parcels["rep"], {c: parcels[c] for c in own_ids if c in parcels})
    parcels = parcels.join(spi)
    addr, addr_method = attach_addresses(parcels)
    parcels = parcels.join(addr)
    has_spi, has_addr = float((parcels["spi"] != "").mean()), float((parcels["address"] != "").mean())
    print(f"  lot-and-plan number found for {has_spi:.0%} of lots ({spi_method}); "
          f"street address for {has_addr:.0%} ({addr_method})")
    id_warnings = []
    if spi_method != "unavailable" and has_spi < 0.9:
        id_warnings.append(f"Lot-and-plan numbers were found for only {has_spi:.0%} of lots.")
    if addr_method != "unavailable" and has_addr < 0.7:
        id_warnings.append(f"Street addresses were found for only {has_addr:.0%} of lots "
                           "(lots without a building, such as parks and car parks, normally have none).")

    # --- Save ----------------------------------------------------------------
    keep = ["parcel_id", "spi", "lot_plan", "address", "address_count", "zone", "area_m2", "built_m2", "coverage", "open_m2", "nearest_station",
            "dist_station_m", "walk_m", "walk_min", "walk_station", "walk_status", "lot_width_m",
            "overlays", "overlays_exclude", "overlays_review", "stacked",
            "sa1", "median_rent_wk", "median_hh_income_wk", "avg_hh_size", "median_age", "rent_to_income",
            "ndvi_mean", "green_frac", "ndbi_mean", "d_ndvi", "d_ndbi", "recent_change", "sat_low_confidence",
            "sat_pixels", "services_method"] + \
           [f"{k}_{x}" for k in C.AMENITIES for x in ("m", "name")] + ["geometry"]
    out = gpd.GeoDataFrame(parcels[[c for c in keep if c in parcels.columns]], geometry="geometry", crs=C.CRS_METRIC)
    out.to_parquet(C.PROCESSED / "parcels.parquet")
    stations.to_parquet(C.PROCESSED / "stations.parquet")
    gpd.GeoDataFrame(geometry=[boundary], crs=C.CRS_METRIC).to_parquet(C.PROCESSED / "boundary.parquet")

    cand = out[out["zone"].fillna("").str.startswith(C.CANDIDATE_ZONE_PREFIXES)]
    warnings_ = list(id_warnings)
    no_bldg = float((cand["built_m2"] == 0).mean()) if len(cand) else 0.0
    if no_bldg > C.FOOTPRINT_GAP_WARN:
        warnings_.append(f"{no_bldg:.0%} of residential lots have no building in the footprint data. "
                         "Some footprints are probably missing, which makes those lots look emptier than they are. "
                         "Check candidates against the aerial photo.")
    if len(b_pts) and align_share < 0.80:
        warnings_.append(f"Only {align_share:.0%} of buildings inside the council fall within a lot boundary "
                         "(expected over 80%). The lot and building layers may not line up; building coverage "
                         "figures are unreliable until this is resolved.")
    if out["zone"].isna().mean() > 0.02:
        warnings_.append(f"{out['zone'].isna().mean():.0%} of lots could not be matched to a planning zone.")
    status.processing(parcels=len(out), candidate_zone_parcels=len(cand), stations=len(stations),
                      lots_without_buildings_share=round(no_bldg, 3),
                      buildings_in_council=len(b_in), buildings_within_lots_share=round(align_share, 3),
                      warnings=warnings_)
    for w in warnings_:
        print(f"  [WARNING] {w}")
    print(f"Done. {len(out):,} parcels saved; {len(cand):,} in NRZ/GRZ zones.")
    print("Next: start the stack (docker compose up) or the API (uvicorn api.main:app)")


if __name__ == "__main__":
    main()
