"""Lot-and-plan numbers and addresses."""
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, box

from src import config as C
from src import identity as I

P = gpd.read_parquet(C.PROCESSED / "parcels.parquet")


def test_lot_plan_label():
    assert I.lot_plan_label("12\\LP12345") == "Lot 12 on LP12345"
    assert I.lot_plan_label("CM1\\PS123456X") == "Common property CM1 on PS123456X"
    assert I.lot_plan_label("2001\\PP3412") == "Lot 2001 on PP3412"
    assert I.lot_plan_label("weird") == "weird"


def test_spi_and_addresses_on_processed_lots():
    assert (P["spi"] != "").mean() == 1.0
    one = P.iloc[0]
    assert one["spi"] == f"{int(one['parcel_id']) - 400000000}\\LP{100000 + (int(one['parcel_id']) - 400000000) // 20}"
    assert one["lot_plan"].startswith("Lot ") and " on LP" in one["lot_plan"]
    assert 0.9 < (P["address"] != "").mean() < 1.0             # every 25th lot has no address
    assert P["address"].str.contains("Test Street Coburg 3058").any()
    assert (P["address_count"] == 2).sum() > 50                # every 10th lot has two
    two = P[P["address_count"] == 2].iloc[0]["address"]
    assert "A " not in two.split(" Test")[0]                   # lowest number (12, not 12A) chosen as main


def test_location_fallback_when_ids_dont_match(tmp_path, monkeypatch):
    lots = gpd.GeoDataFrame({"parcel_id": ["a", "b"]}, geometry=[box(0, 0, 10, 10), box(20, 0, 30, 10)], crs=C.CRS_METRIC)
    det = gpd.GeoDataFrame({"parcel_pfi": ["x", "y"], "parcel_spi": ["1\\LP1", "2\\LP1"]},
                           geometry=[box(0, 0, 10, 10), box(20, 0, 30, 10)], crs=C.CRS_METRIC)
    monkeypatch.setattr(I, "DETAIL", tmp_path / "d.parquet")
    det.to_parquet(I.DETAIL)
    out, method = I.attach_spi(lots, lots.representative_point(), lots["parcel_id"])
    assert method == "location" and out["spi"].tolist() == ["1\\LP1", "2\\LP1"]


def test_address_built_from_parts(tmp_path, monkeypatch):
    lots = gpd.GeoDataFrame(geometry=[box(0, 0, 10, 10)], crs=C.CRS_METRIC)
    a = gpd.GeoDataFrame({"blg_unit_id_1": ["3"], "house_number_1": [12], "house_number_2": [None],
                          "road_name": ["SYDNEY"], "road_type": ["ROAD"], "locality_name": ["BRUNSWICK"],
                          "postcode": ["3056"]}, geometry=[Point(5, 5)], crs=C.CRS_METRIC)
    monkeypatch.setattr(I, "ADDRESSES", tmp_path / "a.parquet")
    a.to_parquet(I.ADDRESSES)
    out, _ = I.attach_addresses(lots)
    assert out["address"].iloc[0] == "3/12 Sydney Road, Brunswick 3056"


def test_missing_layers_are_harmless(tmp_path, monkeypatch):
    lots = gpd.GeoDataFrame({"parcel_id": ["a"]}, geometry=[box(0, 0, 10, 10)], crs=C.CRS_METRIC)
    monkeypatch.setattr(I, "DETAIL", tmp_path / "none1.parquet")
    monkeypatch.setattr(I, "ADDRESSES", tmp_path / "none2.parquet")
    assert I.attach_spi(lots, lots.representative_point(), lots["parcel_id"])[1] == "unavailable"
    out, m = I.attach_addresses(lots)
    assert m == "unavailable" and out["address"].iloc[0] == ""


def test_spi_from_attribute_table(tmp_path, monkeypatch):
    import pandas as pd
    lots = gpd.GeoDataFrame({"parcel_id": ["1001", "1002"]}, geometry=[box(0, 0, 10, 10), box(20, 0, 30, 10)],
                            crs=C.CRS_METRIC)
    monkeypatch.setattr(I, "DETAIL", tmp_path / "d.parquet")
    pd.DataFrame({"parcel_pfi": ["1002", "1001"], "parcel_spi": ["2\\LP9", "1\\LP9"]}).to_parquet(I.DETAIL)
    out, method = I.attach_spi(lots, lots.representative_point(), lots["parcel_id"])
    assert method.startswith("shared ID") and out["lot_plan"].tolist() == ["Lot 1 on LP9", "Lot 2 on LP9"]


def test_picks_the_id_pairing_that_actually_matches(tmp_path, monkeypatch):
    """Real Vicmap case: the lot layer's 'pfi' is the polygon ID, so it matches the detail table's 'pfi',
    not its 'parcel_pfi'. The code must find that pairing rather than assume one."""
    import pandas as pd
    lots = gpd.GeoDataFrame({"pfi": ["500", "501", "502"], "ufi": ["9", "8", "7"]},
                            geometry=[box(0, 0, 10, 10), box(20, 0, 30, 10), box(40, 0, 50, 10)], crs=C.CRS_METRIC)
    monkeypatch.setattr(I, "DETAIL", tmp_path / "d.parquet")
    pd.DataFrame({"pfi": [500, 501, 502], "parcel_pfi": [11, 12, 13], "ufi": [1, 2, 3],
                  "parcel_spi": ["5\\TP1", "6\\TP1", "CM\\PS7"]}).to_parquet(I.DETAIL)
    out, method = I.attach_spi(lots, lots.representative_point(), {"pfi": lots["pfi"], "ufi": lots["ufi"]})
    assert method == "shared ID (pfi = pfi)"
    assert out["spi"].tolist() == ["5\\TP1", "6\\TP1", "CM\\PS7"]
    assert out["lot_plan"].tolist()[:2] == ["Lot 5 on TP1", "Lot 6 on TP1"]
