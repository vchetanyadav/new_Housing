"""Creates a small synthetic dataset in data/raw/ with the same shape as the real downloads.

Used only to test the pipeline without internet:  python -m tests.make_fake_data
Delete data/raw/ afterwards before running the real ingest.
"""
import geopandas as gpd
import numpy as np
from shapely.geometry import Point, box

from src import config as C

rng = np.random.default_rng(42)
x0, y0 = 320_000, 5_820_000  # MGA55 metres near Coburg
parcels, bld = [], []
pid = 0
for i in range(40):
    for j in range(25):
        w = rng.choice([15, 18, 20, 25])
        x, y = x0 + i * 26, y0 + j * 45
        geom = box(x, y, x + w, y + 40)
        pid += 1
        parcels.append({"pfi": str(400000000 + pid), "geometry": geom})   # like Vicmap parcel_view
        cov = rng.uniform(0.1, 0.6)
        bw = w * 0.8
        bld.append(box(x + 1, y + 2, x + 1 + bw, y + 2 + 40 * cov / 0.8))
parcels.append({"pfi": "ROAD", "geometry": box(x0 - 20, y0, x0 - 12, y0 + 1200)})  # thin road casement

crs = C.CRS_METRIC
C.RAW.mkdir(parents=True, exist_ok=True)
gpd.GeoDataFrame(parcels, crs=crs).to_parquet(C.RAW / "parcels.parquet")

# Parcel detail (like Vicmap v_parcel_mp): same lots with lot-and-plan numbers
_det = [p for p in parcels if p["pfi"] != "ROAD"]
gpd.GeoDataFrame({"parcel_pfi": [p["pfi"] for p in _det],
                  "parcel_spi": [f"{int(p['pfi']) - 400000000}\\LP{100000 + (int(p['pfi']) - 400000000) // 20}"
                                 for p in _det]},
                 geometry=[p["geometry"] for p in _det], crs=crs).to_parquet(C.RAW / "parcel_detail.parquet")

# Address points (like Vicmap Address): one per lot, two on every 10th lot, none on every 25th
_addr, _pts = [], []
for i, p in enumerate(_det):
    if i % 25 == 24:
        continue
    c = p["geometry"].centroid
    _addr.append(f"{i % 120 + 1} TEST STREET COBURG 3058"); _pts.append(c)
    if i % 10 == 0:
        _addr.append(f"{i % 120 + 1}A TEST STREET COBURG 3058"); _pts.append(Point(c.x + 2, c.y + 2))
gpd.GeoDataFrame({"ezi_address": _addr}, geometry=_pts, crs=crs).to_parquet(C.RAW / "addresses.parquet")
gpd.GeoDataFrame({"zone_code": ["NRZ1", "GRZ2", "C1Z"], "lga": ["MERRI-BEK"] * 3},
                 geometry=[box(x0 - 30, y0 - 10, x0 + 520, y0 + 1200),
                           box(x0 + 520, y0 - 10, x0 + 1050, y0 + 1000),
                           box(x0 + 520, y0 + 1000, x0 + 1050, y0 + 1200)], crs=crs
                 ).to_parquet(C.RAW / "zones.parquet")
gpd.GeoDataFrame({"zone_code": ["HO123", "SCO1", "NCO2", "DCPO1", "LSIO"]},
                 geometry=[box(x0 + 100, y0 + 100, x0 + 300, y0 + 300),      # heritage precinct -> exclude
                           box(x0, y0, x0 + 50, y0 + 50),                    # specific controls -> review
                           box(x0 + 600, y0 + 600, x0 + 800, y0 + 900),      # neighbourhood character -> review
                           box(x0 - 50, y0 - 50, x0 + 1100, y0 + 1300),      # contributions -> ignored
                           box(x0 + 300, y0 + 700, x0 + 1100, y0 + 701.5)],  # 1.5 m flood sliver -> under 5%, ignored
                 crs=crs).to_parquet(C.RAW / "overlays.parquet")
gpd.GeoDataFrame({"stop_id": ["1", "2"], "stop_name": ["Coburg Station", "Batman Station"]},
                 geometry=[Point(x0 + 300, y0 + 500), Point(x0 + 900, y0 + 1100)], crs=crs
                 ).to_parquet(C.RAW / "stations.parquet")
gpd.GeoDataFrame({"osm_id": range(len(bld))}, geometry=bld, crs=crs).to_parquet(C.RAW / "buildings.parquet")
# Services: a supermarket near Coburg, others spread around; emergency services further out
_am = [("supermarket", "Coles Test", "Coles", x0 + 250, y0 + 450), ("supermarket", "Aldi Test", "ALDI", x0 + 950, y0 + 1150),
       ("market", "Test Market", None, x0 + 400, y0 + 600), ("pharmacy", "Test Chemist", None, x0 + 300, y0 + 520),
       ("gp", "Test Medical", None, x0 + 320, y0 + 560), ("school", "Test Primary", None, x0 + 150, y0 + 900),
       ("childcare", "Test Kinder", None, x0 + 500, y0 + 300), ("park", "Test Park", None, x0 + 100, y0 + 100),
       ("hospital", "Test Hospital", None, x0 + 3000, y0 + 3000), ("police", "Test Police", None, x0 + 600, y0 + 1300),
       ("fire", "Test Fire", None, x0 - 800, y0 + 400), ("ambulance", "Test Ambulance", None, x0 + 1500, y0 - 500)]
gpd.GeoDataFrame({"category": [a[0] for a in _am], "name": [a[1] for a in _am], "brand": [a[2] for a in _am],
                  "emergency_dept": [True if a[0] == "hospital" else None for a in _am],
                  "source": "synthetic"}, geometry=[Point(a[3], a[4]) for a in _am], crs=crs
                 ).to_parquet(C.RAW / "amenities.parquet")
from src import status as _status
for _n in ("parcels", "zones", "overlays", "stations", "buildings", "census", "amenities", "parcel_detail",
           "addresses"):
    _status.source(_n, label=f"{_n} (DEMO DATA)", url="synthetic", rows=0, state="warning",
                   note="Synthetic demo data, not real. Run the full pipeline for real data.")
print(f"Fake data written to {C.RAW}")

# ---------------------------------------------------------------- Phase 2 fakes
import json

import networkx as nx
import osmnx as ox
from pyproj import Transformer

from src import satellite as S

# Census: two SA1s split east/west, different rent pressure
gpd.GeoDataFrame({"SA1_CODE21": ["20601110101", "20601110102"],
                  "Median_rent_weekly": [420, 520], "Median_tot_hhd_inc_weekly": [2100, 1500],
                  "Average_household_size": [2.6, 2.2], "Median_age_persons": [36, 31]},
                 geometry=[box(x0 - 100, y0 - 100, x0 + 520, y0 + 1300), box(x0 + 520, y0 - 100, x0 + 1100, y0 + 1300)],
                 crs=crs).to_parquet(C.RAW / "sa1_census.parquet")

# Walking network: 50 m street grid with a rail corridor at x = x0+600 crossable only at y0+1100
to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
G = nx.MultiDiGraph(crs="EPSG:4326")
step, xs, ys = 50, range(-50, 1101, 50), range(-50, 1251, 50)
for gx in xs:
    for gy in ys:
        lon, lat = to_ll.transform(x0 + gx, y0 + gy)
        G.add_node((gx, gy), x=lon, y=lat)
nid = {n: i for i, n in enumerate(G.nodes)}
G = nx.relabel_nodes(G, nid)
inv = {v: k for k, v in nid.items()}
for n, (gx, gy) in list(inv.items()):
    for dx, dy in ((step, 0), (0, step)):
        nb = (gx + dx, gy + dy)
        if nb in nid:
            crosses_rail = dx and gx < 600 <= gx + dx and gy != 1100
            if not crosses_rail:
                G.add_edge(n, nid[nb], length=float(step)); G.add_edge(nid[nb], n, length=float(step))
ox.save_graphml(G, C.RAW / "walk_network.graphml")

# Satellite: NDVI high in the west, two "construction" lots where NDVI fell and NDBI rose
C.PROCESSED.mkdir(parents=True, exist_ok=True)
T, W, H = S.study_grid(10.0)
cols_x = T.c + (np.arange(W) + 0.5) * 10
rows_y = T.f - (np.arange(H) + 0.5) * 10
X, Y = np.meshgrid(cols_x, rows_y)
ndvi_b = np.where(X < x0 + 520, 0.55, 0.30).astype("float32")
ndbi_b = np.full_like(ndvi_b, -0.10)
ndvi_c, ndbi_c = ndvi_b.copy(), ndbi_b.copy()
changed = ((X > x0 + 130) & (X < x0 + 170) & (Y > y0 + 900) & (Y < y0 + 1000))
ndvi_c[changed], ndbi_c[changed] = 0.10, 0.10
rgb = {k: np.full_like(ndvi_b, v) for k, v in (("red", 0.06), ("green", 0.08), ("blue", 0.05))}
S.write_stack(C.PROCESSED / "sat_baseline.tif", {"ndvi": ndvi_b, "ndbi": ndbi_b, **rgb}, T)
S.write_stack(C.PROCESSED / "sat_current.tif", {"ndvi": ndvi_c, "ndbi": ndbi_c, **rgb}, T)
bounds = S.write_truecolour(rgb, T, C.PROCESSED / "sat_truecolour.png")
(C.PROCESSED / "sat_meta.json").write_text(json.dumps({
    "provider": "dea", "as_at": "2026-10-19", "png_bounds": bounds,
    "current": [{"date": "2026-10-12", "scenes": ["fake"], "clear_share": 1.0}],
    "baseline": [{"date": "2025-10-10", "scenes": ["fake"], "clear_share": 1.0}],
    "attribution": "FAKE TEST DATA"}))
print("Phase 2 fakes written (census, walk network, satellite)")
