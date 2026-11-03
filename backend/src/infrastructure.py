"""Phase 2 - Infrastructure demand from a rezoning scenario.

This module estimates ADDITIONAL DEMAND only. It does not know how much spare capacity
exists in local water mains, zone substations or schools - that information sits with
water corporations, electricity distributors and the Department of Education. If a planner
enters a capacity figure, the app shows demand as a share of it; otherwise it shows demand alone.

All rates come from config.INFRA_ASSUMPTIONS and are shown in the app and the brief.
"""
from __future__ import annotations

import pandas as pd

from . import config as C


def demand(eligible: pd.DataFrame, assumptions: dict | None = None) -> dict:
    a = {**C.INFRA_ASSUMPTIONS, **(assumptions or {})}
    dw = eligible["new_dwellings"].astype(float)
    if a["persons_per_new_dwelling"] is None and "avg_hh_size" in eligible:
        pph = eligible["avg_hh_size"].fillna(eligible["avg_hh_size"].median()).fillna(2.5)
        people = float((dw * pph).sum())
        pph_note = "Census average household size of each lot's SA1"
    else:
        rate = a["persons_per_new_dwelling"] or 2.5
        people = float(dw.sum() * rate)
        pph_note = f"{rate} persons per new dwelling"
    total_dw = int(dw.sum())
    return {
        "new_dwellings": total_dw,
        "new_residents": round(people),
        "water_kl_day": round(people * a["water_l_per_person_day"] / 1000, 1),
        "wastewater_kl_day": round(people * a["wastewater_l_per_person_day"] / 1000, 1),
        "peak_mva": round(total_dw * a["peak_kva_per_dwelling"] / 1000, 2),
        "primary_students": round(total_dw * a["primary_students_per_dwelling"]),
        "secondary_students": round(total_dw * a["secondary_students_per_dwelling"]),
        "assumptions": {**a, "persons_per_new_dwelling": pph_note},
    }


def demand_by_area(eligible: pd.DataFrame, by: str = "walk_station") -> pd.DataFrame:
    rows = []
    for key, grp in eligible.groupby(by):
        d = demand(grp)
        d.pop("assumptions")
        rows.append({by: key, **d})
    return pd.DataFrame(rows).sort_values("new_dwellings", ascending=False) if rows else pd.DataFrame()
