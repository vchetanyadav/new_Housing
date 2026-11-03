"""API tests. Run after `python -m tests.make_fake_data && python -m src.process`:  pytest -q"""
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health_and_meta():
    client.get("/api/meta")  # make sure data is loaded
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["data_loaded"] and h["parcels"] == 1000
    m = client.get("/api/meta").json()
    assert m["flags"] == {"walking": True, "satellite": True, "census": True, "services": True, "satellite_image": True}
    assert m["satellite"]["current"][0]["date"] == "2026-10-12"


def test_layers():
    assert len(client.get("/api/layers/stations").json()["features"]) == 2
    assert client.get("/api/layers/boundary").json()["type"] == "FeatureCollection"
    assert len(client.get("/api/layers/changed").json()["features"]) >= 1
    r = client.get("/api/layers/satellite.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_scenario_defaults_and_validation():
    r = client.post("/api/scenario", json={})
    assert r.status_code == 200 and "X-Request-ID" in r.headers
    d = r.json()
    assert d["summary"]["eligible_lots"] == len(d["lots"]["features"]) > 0
    assert d["summary"]["new_dwellings"] == d["infrastructure"]["new_dwellings"]
    assert sum(x["lots"] for x in d["by_station"]) == d["summary"]["eligible_lots"]
    bad = client.post("/api/scenario", json={"scenario": {"radius_m": -5}})
    assert bad.status_code == 422


def test_scenario_responds_to_parameters():
    small = client.post("/api/scenario", json={"scenario": {"radius_m": 400}}).json()["summary"]
    big = client.post("/api/scenario", json={"scenario": {"radius_m": 1200}}).json()["summary"]
    assert small["eligible_lots"] < big["eligible_lots"]
    dense = client.post("/api/scenario", json={"scenario": {"dwellings_per_ha": 150}}).json()["summary"]
    base = client.post("/api/scenario", json={}).json()["summary"]
    assert dense["new_dwellings"] > base["new_dwellings"]


def test_parcel_detail_and_search():
    pid = client.post("/api/scenario", json={}).json()["lots"]["features"][0]["properties"]["parcel_id"]
    hits = client.get("/api/parcels/search", params={"q": pid}).json()["results"]
    assert pid in [h["parcel_id"] for h in hits]
    d = client.post("/api/parcels/detail", json={"parcel_id": pid}).json()
    assert d["parcel"]["eligible"] is True and d["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert client.post("/api/parcels/detail", json={"parcel_id": "nope"}).status_code == 404


def test_exports():
    csv = client.post("/api/export/csv", json={})
    assert csv.status_code == 200 and csv.text.startswith("parcel_id,")
    gj = client.post("/api/export/geojson", json={}).json()
    assert gj["type"] == "FeatureCollection"
    md = client.post("/api/export/brief", json={}).text
    assert "Scenario Brief" in md
    assert client.post("/api/export/xlsx", json={}).status_code == 400


def test_data_status_and_warnings():
    st = client.get("/api/data-status").json()
    assert st["overall"] in ("ok", "warning", "failed") and "parcels" in st["sources"]
    d = client.post("/api/scenario", json={"scenario": {"min_width_m": 18, "exclude_review_overlays": True}}).json()
    assert isinstance(d["warnings"], list) and "needs_review" in d["summary"]
    assert any(e["reason"] == "lot too narrow" for e in d["exclusions"])


def test_services_in_api():
    m = client.get("/api/meta").json()
    assert m["flags"]["services"] and "supermarket" in m["amenities"]["categories"]
    layer = client.get("/api/layers/amenities").json()
    assert {"Coles Test", "Test Hospital"} <= {f["properties"]["name"] for f in layer["features"]}
    d = client.post("/api/scenario", json={}).json()
    assert "supermarket" in d["summary"]["services_access"]
    pid = d["lots"]["features"][0]["properties"]["parcel_id"]
    det = client.post("/api/parcels/detail", json={"parcel_id": pid}).json()["parcel"]["services"]
    sm = next(x for x in det if x["key"] == "supermarket")
    hp = next(x for x in det if x["key"] == "hospital")
    assert sm["method"] == "walking network" and isinstance(sm["within_reach"], bool)
    assert hp["method"] == "straight line" and hp["within_reach"] is None
    assert "Access to everyday needs" in client.post("/api/export/brief", json={}).text


def test_search_by_address_and_lot_plan():
    r = client.get("/api/parcels/search", params={"q": "test street"}).json()["results"]
    assert r and r[0]["address"].endswith("Test Street Coburg 3058") and -38 < r[0]["lat"] < -37
    spi_hit = client.get("/api/parcels/search", params={"q": r[0]["lot_plan"].split(" on ")[1]}).json()["results"]
    assert spi_hit
    det = client.post("/api/parcels/detail", json={"parcel_id": r[0]["parcel_id"]}).json()["parcel"]
    assert det["address"] and det["lot_plan"].startswith("Lot ") and "\\LP" in det["spi"]
    csv = client.post("/api/export/csv", json={}).text.splitlines()[0]
    assert csv.startswith("parcel_id,spi,lot_plan,address,address_count,")
