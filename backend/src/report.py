"""Scenario brief generated from the actual results of a run - no hand-typed numbers."""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd

from . import config as C


def _fmt(n, d=0):
    return f"{n:,.{d}f}" if pd.notna(n) else "n/a"


def brief(df: pd.DataFrame, summ: dict, s, infra: dict, sat_meta: dict | None) -> str:
    e = df[df["eligible"]]
    method = df.attrs.get("access_method", "straight line")
    sat = "Not used in this run."
    if sat_meta:
        cur = ", ".join(u["date"] for u in sat_meta["current"])
        base = ", ".join(u["date"] for u in sat_meta.get("baseline", [])) or "none"
        sat = (f"Sentinel-2 composite from passes on {cur} (baseline: {base}). "
               f"{sat_meta['attribution']}.")
    excl = "\n".join(f"| {k} | {_fmt(v)} |" for k, v in summ["exclusions"].items())
    stations = "\n".join(f"| {k} | {_fmt(r.lots)} | {_fmt(r.new_dwellings)} |"
                         for k, r in summ["by_station"].head(15).iterrows())
    a = infra["assumptions"]
    rent = e["rent_to_income"].median() if "rent_to_income" in e and len(e) else float("nan")

    from . import status as _status
    warns = list(df.attrs.get("warnings", [])) + list(_status.load().get("processing", {}).get("warnings", []))
    warn_md = "\n".join(f"- {w}" for w in warns) if warns else "None recorded for this run."
    sa = summ.get("services_access", {})
    if sa and len(e):
        method = e["services_method"].iloc[0] if "services_method" in e else "walking"
        rows = [f"| {C.AMENITIES[k]['label']} | {v:.0%} |" for k, v in sa.items()]
        emerg = [f"| {C.AMENITIES[k]['label']} | {e[k + '_m'].median() / 1000:.1f} km |"
                 for k, v in C.AMENITIES.items() if v["kind"] == "emergency" and f"{k}_m" in e]
        svc_md = (f"Share of eligible lots with each everyday need within {s.services_walk_m:,.0f} m ({method}), "
                  "after Plan Melbourne's 20-minute neighbourhood idea:\n\n| Everyday need | Eligible lots within reach |\n"
                  "|---|---|\n" + "\n".join(rows) +
                  "\n\nMedian straight-line distance from eligible lots to the nearest emergency service:\n\n"
                  "| Service | Median distance |\n|---|---|\n" + "\n".join(emerg) +
                  "\n\nLocations from OpenStreetMap; check completeness locally. Hospitals are those recorded as "
                  "having an emergency department where that is recorded.")
    else:
        svc_md = "Services data was not available for this run."
    return f"""# Smart Housing & Zoning Optimiser — Scenario Brief

**Study area:** Merri-bek City Council (pilot) · **Generated:** {dt.date.today().isoformat()}
**Status:** Research prototype. Identifies *opportunities for investigation*; it does not determine
whether land can or should be rezoned.

## Scenario tested
- Zones considered for upzoning: {", ".join(s.zones)}
- Station catchment: {_fmt(s.radius_m)} m ({method} distance)
- Minimum lot size: {_fmt(s.min_lot_m2)} m² · minimum lot width: {_fmt(s.min_width_m)} m · maximum existing building coverage: {s.max_coverage:.0%}
- Target density: {_fmt(s.dwellings_per_ha)} dwellings / ha
- Excluding overlays (heritage, flood, contamination, acquisition, bushfire) applied: {"yes" if s.exclude_overlays else "no"}
- Overlays needing review (character, design, vegetation, landscape, etc.) excluded: {"yes" if s.exclude_review_overlays else "no — such lots are counted but flagged"}
- Lots with satellite-detected recent clearing/construction excluded: {"yes" if s.exclude_recent_change else "no"}

## Data warnings
{warn_md}

## Results
| Measure | Value |
|---|---|
| Eligible lots | {_fmt(summ["eligible_lots"])} |
| Theoretical additional dwellings | {_fmt(summ["new_dwellings"])} |
| Eligible lots needing planner review (overlay) | {_fmt(summ.get("needs_review", 0))} |
| Land on eligible lots | {_fmt(summ["land_ha"], 1)} ha |
| Unbuilt land on eligible lots | {_fmt(summ["open_land_ha"], 1)} ha |
| Median rent-to-income ratio (eligible lots' SA1s) | {_fmt(rent, 2)} |

### By station (top 15)
| Station | Lots | Additional dwellings |
|---|---|---|
{stations}

### Why lots were excluded
| Reason | Lots |
|---|---|
{excl}

## Access to everyday needs and emergency services
{svc_md}

## Indicative infrastructure demand
| Item | Estimate |
|---|---|
| New residents | {_fmt(infra["new_residents"])} |
| Water | {_fmt(infra["water_kl_day"], 1)} kL/day |
| Wastewater | {_fmt(infra["wastewater_kl_day"], 1)} kL/day |
| Peak electricity | {_fmt(infra["peak_mva"], 2)} MVA |
| Primary / secondary students | {_fmt(infra["primary_students"])} / {_fmt(infra["secondary_students"])} |

Rates used (placeholders, to be confirmed with the water corporation, distributor and
Department of Education): water {a["water_l_per_person_day"]} L/person/day; wastewater
{a["wastewater_l_per_person_day"]} L/person/day; {a["peak_kva_per_dwelling"]} kVA/dwelling peak;
{a["primary_students_per_dwelling"]} primary and {a["secondary_students_per_dwelling"]} secondary
students per dwelling; residents per dwelling from {a["persons_per_new_dwelling"]}.
These are demand estimates only; available network capacity has not been assessed.

## Method
1. Lots from Vicmap Property, zones and overlays from Vicmap Planning (DTP). An overlay counts for a lot
   when it covers at least 5% of the lot's area. Heritage, flood, contamination, acquisition and bushfire
   overlays exclude a lot; character, design, vegetation, landscape and other overlays flag it for review.
   Restrictive covenants, easements and other title constraints are not in open data and are not assessed.
2. Building coverage = OpenStreetMap building footprints intersected with each lot.
3. Access = {method} distance to the nearest metro train station (Transport Victoria GTFS).
4. Demand = ABS 2021 Census G02 median rent ÷ median household income for the lot's SA1 (a proxy).
5. Satellite = {sat}
   Per-lot vegetation (NDVI) and built-up (NDBI) indices; a fall in NDVI with a rise in NDBI
   versus a year earlier flags lots that appear to have been cleared or built on recently.
6. Score = weighted mix of proximity ({s.w_proximity}), lot size ({s.w_lot_size}),
   unbuilt land ({s.w_open_land}), rental pressure ({s.w_demand}) and everyday services within reach
   ({s.w_services}; share of supermarket, pharmacy, GP, school, childcare and park within
   {s.services_walk_m:,.0f} m), normalised to 0–1.
7. Additional dwellings = lot area × target density − the existing dwelling. Lots narrower than the
   minimum width (shorter side of the lot's bounding rectangle) are excluded.

## Limitations
- Theoretical capacity, not a forecast: ownership, lot consolidation, market feasibility,
  setbacks, access, tree protection and design controls are not modelled.
- Sentinel-2 pixels are 10 m; lot-level satellite measures are indicative, especially on small lots.
- OpenStreetMap footprints may be incomplete or out of date.
- Census figures date from August 2021.

## Data sources & licences
Vicmap Property & Vicmap Planning (DTP, CC BY 3.0 AU) · GTFS Schedule (Transport Victoria, CC BY 4.0) ·
OpenStreetMap (© contributors, ODbL) · ABS Census 2021 & ASGS (CC BY 4.0) · Sentinel-2 via
{"Digital Earth Australia (CC BY 4.0)" if (sat_meta or {}).get("provider") == "dea" else "Copernicus / Earth Search"}.
"""


def load_sat_meta():
    p = C.PROCESSED / "sat_meta.json"
    return json.loads(p.read_text()) if p.exists() else None
