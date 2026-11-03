"""Services download behaviour against simulated servers (no network needed)."""
import geopandas as gpd
import pytest
from shapely.geometry import Point

from src import amenities as A
from src import config as C


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Gateway Timeout")

    def json(self):
        return self._d


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "OUT", tmp_path / "amenities.parquet")
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    return tmp_path / "amenities.parquet"


def test_overture_category_old_and_new_formats():
    assert A._overture_category_of({"categories": {"primary": "supermarket", "alternate": []}})[0] == "supermarket"
    assert A._overture_category_of({"basic_category": "pharmacy"})[0] == "pharmacy"
    assert A._overture_category_of({"taxonomy": {"primary": "x", "hierarchy": ["health", "hospital"]}})[0] == "hospital"
    assert A._overture_category_of({"categories": {"primary": "cafe"}})[0] is None


def test_busy_osm_categories_filled_from_overture(out, monkeypatch):
    def post(url, data=None, **kw):
        if 'shop"="supermarket' in data["data"]:          # only supermarkets get through
            return Resp({"elements": [{"type": "node", "lat": -37.74, "lon": 144.96,
                                       "tags": {"shop": "supermarket", "brand": "Coles", "name": "Coles Coburg"}}]})
        return Resp({}, status=504)
    monkeypatch.setattr(A.requests, "post", post)
    monkeypatch.setattr(A, "_overture_places", lambda keys: [
        {"category": k, "name": f"OV {k}", "brand": None, "emergency_dept": None, "source": "Overture Maps",
         "lon": 144.95, "lat": -37.73} for k in keys])
    n, src, counts = A.fetch_amenities()
    assert counts["supermarket"] == 1 and all(counts[k] == 1 for k in C.AMENITIES)
    assert "OpenStreetMap (1 categories)" in src and "Overture Maps" in src
    g = gpd.read_parquet(out)
    assert g.loc[g.category == "supermarket", "name"].iloc[0] == "Coles Coburg"


def test_everything_down_keeps_previous_file(out, monkeypatch):
    gpd.GeoDataFrame({"category": ["park"]}, geometry=[Point(0, 0)], crs=C.CRS_METRIC).to_parquet(out)
    monkeypatch.setattr(A.requests, "post", lambda *a, **k: Resp({}, status=504))
    monkeypatch.setattr(A, "_overture_places", lambda keys: (_ for _ in ()).throw(RuntimeError("S3 down")))
    with pytest.raises(RuntimeError, match="no services could be downloaded"):
        A.fetch_amenities()
    assert len(gpd.read_parquet(out)) == 1


def test_osm_park_query_skips_nodes_and_hospital_flag(out, monkeypatch):
    seen = []

    def post(url, data=None, **kw):
        seen.append(data["data"])
        if '"amenity"="hospital"' in data["data"]:
            return Resp({"elements": [{"type": "way", "center": {"lat": -37.75, "lon": 144.97},
                                       "tags": {"amenity": "hospital", "emergency": "yes", "name": "Test ED"}}]})
        return Resp({"elements": []})
    monkeypatch.setattr(A.requests, "post", post)
    n, src, counts = A.fetch_amenities()
    assert counts == {"hospital": 1}
    assert any('way["leisure"="park"]' in q for q in seen) and not any('nwr["leisure"="park"]' in q for q in seen)
    assert bool(gpd.read_parquet(out)["emergency_dept"].iloc[0]) is True
