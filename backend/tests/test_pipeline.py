"""Run after `python -m tests.make_fake_data && python -m src.process`:   pytest -q"""
import datetime as dt
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import rasterio

from src import config as C
from src import satellite as S
from src.infrastructure import demand
from src.report import brief, load_sat_meta
from src.scoring import Scenario, score, summarise

P = gpd.read_parquet(C.PROCESSED / "parcels.parquet")


def test_walking_never_shorter_than_straight_line():
    ok = P["walk_m"].notna()
    assert ok.mean() > 0.95
    assert (P.loc[ok, "walk_m"] >= P.loc[ok, "dist_station_m"] - 1).all()


def test_rail_barrier_detours():
    # Lots just west of the fake rail line can't cross it directly to Batman (east), so
    # their walk should be much longer than straight-line for at least some lots.
    detour = P["walk_m"] / P["dist_station_m"]
    assert detour.max() > 1.4


def test_census_joined_and_ratio():
    assert P["sa1"].notna().all()
    assert set(P["rent_to_income"].round(3)) == {round(420 / 2100, 3), round(520 / 1500, 3)}


def test_satellite_flags_changed_lots_only_there():
    changed = P[P["recent_change"]]
    assert 1 <= len(changed) <= 10
    assert (changed["d_ndvi"] < -C.CHANGE_NDVI_DROP).all()
    west = P.geometry.centroid.x < P.geometry.centroid.x.min() + 400
    assert P.loc[west & ~P["recent_change"], "green_frac"].median() > 0.95  # neighbours blur at 10 m


def test_scenario_rules_and_yield():
    s = Scenario()
    df = score(P, s)
    e = df[df["eligible"]]
    assert (e["access_m"] <= s.radius_m).all()
    assert (e["coverage"] <= s.max_coverage).all()
    assert not e["recent_change"].any()
    assert (e["new_dwellings"] == np.floor(e["area_m2"] * s.dwellings_per_ha / 1e4) - 1).all()
    assert "satellite: recent clearing / construction" in set(df["excluded_reason"]) or \
        df.loc[df["recent_change"], "excluded_reason"].ne("").all()
    # straight-line catchment must admit at least as many lots as walking
    s2 = Scenario(use_walking=False)
    assert summarise(score(P, s2))["eligible_lots"] >= summarise(df)["eligible_lots"]


def test_infrastructure_and_brief():
    s = Scenario()
    df = score(P, s)
    e = df[df["eligible"]]
    d = demand(e)
    assert d["new_dwellings"] == e["new_dwellings"].sum()
    assert d["water_kl_day"] == round(d["new_residents"] * 160 / 1000, 1) or abs(
        d["water_kl_day"] - d["new_residents"] * 0.16) < 0.2
    md = brief(df, summarise(df), s, d, load_sat_meta())
    assert f"{d['new_dwellings']:,}" in md and "2026-10-12" in md


def test_composite_reads_real_geotiffs(tmp_path):
    """Exercise the STAC read path with local GeoTIFFs standing in for DEA assets."""
    T, W, H = S.study_grid(10.0)
    vals = {"nbart_red": 600, "nbart_green": 700, "nbart_blue": 500, "nbart_nir_1": 3000,
            "nbart_swir_2": 1500, "oa_fmask": 1}
    assets = {}
    for name, v in vals.items():
        path = tmp_path / f"{name}.tif"
        arr = np.full((H, W), v, "int16" if name != "oa_fmask" else "uint8")
        if name == "oa_fmask":
            arr[: H // 2] = 2  # top half cloudy
        with rasterio.open(path, "w", driver="GTiff", width=W, height=H, count=1, dtype=arr.dtype,
                           crs=C.CRS_METRIC, transform=T, nodata=0 if name != "oa_fmask" else None) as dst:
            dst.write(arr, 1)
        assets[name] = SimpleNamespace(href=str(path))
    item = SimpleNamespace(id="fake", assets=assets)
    bands, _, used = S.composite("dea", [(dt.date(2026, 10, 12), [item])])
    idx = S.indices(bands)
    assert 0.45 < used[0]["clear_share"] < 0.55          # cloud mask applied
    assert np.isnan(idx["ndvi"][: H // 2 - 2]).all()     # cloudy half masked
    assert abs(np.nanmean(idx["ndvi"]) - (0.3 - 0.06) / (0.3 + 0.06)) < 1e-3


# ---------------------------------------------------------------- review fixes (overlays, width, walking, status)
from src.process import overlay_category, overlay_type  # noqa: E402


def test_overlay_codes_classified():
    assert overlay_type("HO123") == "HO" and overlay_type("ddo5") == "DDO" and overlay_type("LSIO") == "LSIO"
    assert overlay_category("HO7") == "exclude" and overlay_category("SBO1") == "exclude"
    assert overlay_category("NCO2") == "review" and overlay_category("ZZO9") == "review"  # unknown -> review
    assert overlay_category("DCPO1") == "ignore"


def test_overlays_by_share_of_lot():
    assert (P["overlays_exclude"].str.contains("HO123")).sum() > 10
    assert P["overlays_review"].str.contains("NCO2").any()
    assert not P["overlays"].str.contains("DCPO").any()          # contribution overlays ignored
    assert not P["overlays"].str.contains("LSIO").any()          # 1.5 m sliver < 5% of every lot
    assert P["overlays"].str.contains(r"\(\d+%\)").any()


def test_review_overlays_flagged_not_excluded_by_default():
    df = score(P, Scenario())
    flagged = df[df["needs_review"]]
    assert len(flagged) > 0 and flagged["eligible"].all()
    strict = score(P, Scenario(exclude_review_overlays=True))
    assert not strict["needs_review"].any()
    assert (strict["excluded_reason"] == "overlay needing review").sum() == len(flagged)


def test_lot_width_rule():
    assert P["lot_width_m"].between(14, 26).all()   # fake lots are 15-25 m wide
    df = score(P, Scenario(min_width_m=19))
    assert (df["excluded_reason"] == "lot too narrow").sum() > 0
    assert (df.loc[df["eligible"], "lot_width_m"] >= 19).all()


def test_no_silent_straight_line_fallback():
    no_walk = P.assign(walk_m=np.nan, walk_station=None)
    df = score(no_walk, Scenario(use_walking=True))
    assert df.attrs["access_method"].startswith("straight line (walking network unavailable)")
    assert any("Walking distances are unavailable" in w for w in df.attrs["warnings"])
    ok = score(P, Scenario())
    assert ok.attrs["warnings"] == [] and ok.attrs["access_method"] == "walking network"


def test_status_file_written():
    from src import status
    st = status.load()
    assert {"parcels", "zones", "overlays", "stations", "buildings"} <= set(st["sources"])
    assert st["processing"]["parcels"] == len(P)
    assert st["sources"]["walk_network"]["state"] == "ok"


def test_buildings_line_up_with_lots():
    from src import status
    pr = status.load()["processing"]
    assert pr["buildings_within_lots_share"] > 0.9
    assert not any("line up" in w for w in pr["warnings"])


# ---------------------------------------------------------------- everyday needs and emergency services
def test_services_attached_per_lot():
    for k in ("supermarket", "pharmacy", "gp", "school", "childcare", "park", "hospital", "police", "fire", "ambulance"):
        assert f"{k}_m" in P and f"{k}_name" in P
    assert (P["services_method"] == "walking network").all()
    # emergency services are straight-line; Test Hospital is ~3-4 km away
    assert P["hospital_m"].between(1500, 5000).all() and (P["hospital_name"] == "Test Hospital").all()
    # nearest supermarket is the right one on each side, and walking is never shorter than a straight line
    assert set(P["supermarket_name"].dropna()) == {"Coles Test", "Aldi Test"}
    from shapely.geometry import Point
    straight = P.geometry.representative_point().distance(
        gpd.GeoSeries([Point(320_250, 5_820_450)] * len(P), index=P.index, crs=P.crs))
    near_coles = P["supermarket_name"] == "Coles Test"
    assert (P.loc[near_coles, "supermarket_m"] >= straight[near_coles] - 30).all()


def test_services_factor_and_supermarket_rule():
    base = score(P, Scenario())
    assert base["f_services"].between(0, 1).all() and base["services_met"].max() <= 6
    tight = score(P, Scenario(max_supermarket_m=300))
    assert (tight["excluded_reason"] == "no supermarket within reach").sum() > 0
    assert (tight.loc[tight["eligible"], "supermarket_m"] <= 300).all()
    far = score(P, Scenario(services_walk_m=3000))
    assert far["f_services"].mean() > base["f_services"].mean()
