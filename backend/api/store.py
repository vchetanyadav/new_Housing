"""Holds the processed data in memory and reloads it when the pipeline writes new files."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

import geopandas as gpd
import numpy as np
import shapely

from src import config as C

PARCELS = C.PROCESSED / "parcels.parquet"


@dataclass
class Snapshot:
    version: float = 0.0
    parcels: gpd.GeoDataFrame | None = None       # metric CRS, all columns
    geom_wgs: gpd.GeoSeries | None = None         # simplified lon/lat outlines, same index
    stations: dict = field(default_factory=dict)  # GeoJSON
    boundary: dict = field(default_factory=dict)  # GeoJSON
    center: list = field(default_factory=lambda: [145.0, -37.74])
    sat_meta: dict | None = None
    flags: dict = field(default_factory=dict)


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self._snap = Snapshot()

    def peek(self) -> Snapshot:
        """Current data without triggering a (possibly slow) reload - for health checks."""
        return self._snap

    def get(self) -> Snapshot:
        """Current data; transparently reloads if the pipeline has produced a newer file."""
        try:
            mtime = PARCELS.stat().st_mtime
        except FileNotFoundError:
            return self._snap
        if mtime != self._snap.version:
            with self._lock:
                if mtime != self._snap.version:
                    self._snap = self._load(mtime)
        return self._snap

    @staticmethod
    def _load(mtime: float) -> Snapshot:
        p = gpd.read_parquet(PARCELS).reset_index(drop=True)
        stations = gpd.read_parquet(C.PROCESSED / "stations.parquet").to_crs(C.CRS_WGS84)
        boundary = gpd.read_parquet(C.PROCESSED / "boundary.parquet").to_crs(C.CRS_WGS84)
        meta_path = C.PROCESSED / "sat_meta.json"
        c = boundary.geometry.iloc[0].centroid

        def has(col):
            return bool(col in p and p[col].notna().any())

        return Snapshot(
            version=mtime,
            parcels=p,
            geom_wgs=gpd.GeoSeries(shapely.set_precision(p.geometry.simplify(0.3).to_crs(C.CRS_WGS84).values, 1e-6),
                                   index=p.index, crs=C.CRS_WGS84),
            stations=json.loads(stations[["stop_name", "geometry"]].to_json(drop_id=True)),
            boundary=json.loads(boundary.to_json(drop_id=True)),
            center=[round(c.x, 5), round(c.y, 5)],
            sat_meta=json.loads(meta_path.read_text()) if meta_path.exists() else None,
            flags={"walking": has("walk_m"), "satellite": has("ndvi_mean"), "census": has("rent_to_income"),
                   "services": has("supermarket_m"),
                   "satellite_image": (C.PROCESSED / "sat_truecolour.png").exists()},
        )


def clean(v):
    """JSON-safe scalar (NaN -> None, numpy -> python)."""
    if v is None:
        return None
    if isinstance(v, (np.floating, float)):
        return None if np.isnan(v) else round(float(v), 4)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


store = Store()
