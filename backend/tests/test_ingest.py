"""Downloader behaviour against simulated servers (no network needed)."""
import geopandas as gpd
import pytest
from shapely.geometry import Point, box

from src import config as C
from src import ingest


def _feature(i):
    x = 16_130_000 + i * 30
    return {"type": "Feature", "id": f"f.{i}", "properties": {"ufi": i, "pfi": str(1000 + i)},
            "geometry": {"type": "Polygon", "coordinates": [[[x, -4_540_000], [x + 20, -4_540_000],
                                                             [x + 20, -4_539_960], [x, -4_539_960], [x, -4_540_000]]]}}


class FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def make_server(total=23, cap=5, sort_ok=True, honour_start=True):
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(dict(params))
        if "sortBy" in params and not sort_ok:
            raise ingest.SourceError("400 Bad Request: sortBy not supported")
        start = params["startIndex"] if honour_start else 0
        n = min(cap, params["count"], max(total - start, 0))
        return FakeResp({"type": "FeatureCollection", "numberMatched": total,
                         "features": [_feature(i) for i in range(start, start + n)]})
    return fake_get, calls


@pytest.fixture
def raw(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "RAW", tmp_path)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: None)
    return tmp_path


def test_pages_through_server_cap(raw, monkeypatch):
    # The real Vicmap server returned 5,000 of many more; the old code stopped after page one.
    fake, calls = make_server(total=23, cap=5)
    monkeypatch.setattr(ingest, "_get", fake)
    n = ingest.fetch_wfs_layer("x:parcels", "parcels", "parcels", page_size=10)
    assert n == 23 and len(calls) == 5
    assert len(gpd.read_parquet(raw / "parcels.parquet")) == 23


def test_falls_back_when_server_cannot_sort(raw, monkeypatch):
    fake, calls = make_server(total=12, cap=5, sort_ok=False)
    monkeypatch.setattr(ingest, "_get", fake)
    assert ingest.fetch_wfs_layer("x:zones", "zones", "zones") == 12
    assert "sortBy" in calls[0] and all("sortBy" not in c for c in calls[1:])


def test_detects_server_ignoring_paging(raw, monkeypatch):
    fake, _ = make_server(total=12, cap=5, honour_start=False)
    monkeypatch.setattr(ingest, "_get", fake)
    with pytest.raises(ingest.SourceError, match="same page twice"):
        ingest.fetch_wfs_layer("x:zones", "zones", "zones")
    assert not (raw / "zones.parquet").exists()   # nothing half-written


def test_buildings_fall_back_to_osm(raw, monkeypatch):
    monkeypatch.setattr(ingest, "_overture_buildings", lambda: (_ for _ in ()).throw(RuntimeError("S3 blocked")))
    monkeypatch.setattr(ingest, "_osm_buildings", lambda: gpd.GeoDataFrame(
        {"id": ["osm:1"], "source": ["OpenStreetMap"]}, geometry=[box(144.96, -37.74, 144.9602, -37.7398)], crs=C.CRS_WGS84))
    n, url = ingest.fetch_buildings()
    assert n == 1 and "overpass" in url


def test_failed_download_keeps_previous_copy(raw, monkeypatch):
    gpd.GeoDataFrame({"a": [1]}, geometry=[Point(0, 0)], crs=C.CRS_METRIC).to_parquet(raw / "zones.parquet")
    monkeypatch.setattr(ingest, "_get", lambda *a, **k: FakeResp({"features": [], "numberMatched": 0}))
    with pytest.raises(ingest.SourceError):
        ingest.fetch_wfs_layer("x:zones", "zones", "zones")
    assert len(gpd.read_parquet(raw / "zones.parquet")) == 1


def test_parcel_detail_fetches_only_id_and_spi(raw, monkeypatch):
    """The lot-and-plan layer is fetched as a small attribute table when it has a parcel ID."""
    calls = []
    monkeypatch.setattr(ingest, "resolve_layer", lambda pref, kw: "x:v_parcel_mp")
    monkeypatch.setattr(ingest, "describe_layer", lambda layer: {
        "geom": "gml:MultiSurfacePropertyType", "parcel_pfi": "xsd:string", "parcel_spi": "xsd:string",
        "parcel_lga_code": "xsd:int"})

    def fake_get(url, params=None, **kw):
        calls.append(dict(params))
        start = params["startIndex"]
        n = min(5, max(12 - start, 0))
        return FakeResp({"numberMatched": 12, "features": [
            {"type": "Feature", "id": f"v.{i}", "geometry": None,
             "properties": {"parcel_pfi": str(1000 + i), "parcel_spi": f"{i}\\LP1"}} for i in range(start, start + n)]})
    monkeypatch.setattr(ingest, "_get", fake_get)
    n, url, note = ingest.fetch_parcel_detail()
    assert n == 12 and "ID columns ['parcel_pfi']" in note
    assert calls[0]["propertyName"] == "parcel_pfi,parcel_spi" and calls[0]["sortBy"] == "parcel_pfi"
    import pandas as pd
    t = pd.read_parquet(raw / "parcel_detail.parquet")
    assert list(t.columns) == ["parcel_pfi", "parcel_spi"] and t["parcel_spi"].iloc[3] == "3\\LP1"
