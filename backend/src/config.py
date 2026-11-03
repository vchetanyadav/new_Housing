"""Project-wide settings. Change the council here to run the pipeline elsewhere in Victoria."""
from pathlib import Path

import os

ROOT = Path(__file__).resolve().parents[1]
# In Docker the data folder is mounted at /data; locally it defaults to ../data (project root).
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT.parent / "data"))
RAW = DATA_DIR / "raw"
PROCESSED = DATA_DIR / "processed"

# --- Study area -------------------------------------------------------------
# Merri-bek (formerly Moreland) City Council. Names are matched case-insensitively
# against the LGA field in Vicmap Planning, so both spellings are listed.
LGA_NAMES = ["MERRI-BEK", "MERRI BEK", "MERRIBEK", "MORELAND"]

# Lon/lat bounding box slightly larger than the council (EPSG:4326).
# Used to limit downloads; the exact LGA boundary is applied afterwards.
BBOX = (144.885, -37.790, 145.020, -37.680)  # min_lon, min_lat, max_lon, max_lat

# --- Coordinate systems -----------------------------------------------------
CRS_WGS84 = "EPSG:4326"
CRS_METRIC = "EPSG:7855"  # GDA2020 / MGA zone 55 (metres) - Melbourne

# --- Data sources -----------------------------------------------------------
VICMAP_WFS = "https://opendata.maps.vic.gov.au/geoserver/wfs"
LAYER_PARCELS = "open-data-platform:parcel_view"
LAYER_ZONES = "open-data-platform:plan_zone"
LAYER_OVERLAYS = "open-data-platform:plan_overlay"
# Lot-and-plan numbers (SPI) and street addresses - optional, shown to planners instead of internal IDs.
# If a name changes, the downloader looks for a similarly named layer in the service's catalogue.
LAYER_PARCEL_DETAIL = "open-data-platform:v_parcel_mp"
LAYER_ADDRESS = "open-data-platform:address"

GTFS_URL = "https://data.ptv.vic.gov.au/downloads/gtfs.zip"
# If GTFS_URL stops working, the downloader asks the Transport Victoria open-data portal (CKAN API)
# for the current file link of the "gtfs-schedule" dataset.
GTFS_CKAN = "https://opendata.transport.vic.gov.au/api/3/action/package_show?id=gtfs-schedule"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Overpass API servers tried in order for the walking network (osmnx wants the base URL without /interpreter)
OVERPASS_API_BASES = ["https://overpass-api.de/api", "https://overpass.kumi.systems/api",
                      "https://overpass.private.coffee/api"]
# Building footprints: "overture" (OpenStreetMap + Microsoft + Esri footprints; far more complete for
# Melbourne houses) with automatic fallback to "osm" (OpenStreetMap only).
BUILDINGS_SOURCE = os.environ.get("BUILDINGS_SOURCE", "overture")

# --- Planning rules ---------------------------------------------------------
# Zones treated as low-density residential and therefore candidates for upzoning.
CANDIDATE_ZONE_PREFIXES = ("NRZ", "GRZ")
# Planning overlays, grouped by what they mean for upzoning. The code is the overlay's letters
# (HO123 -> HO). An overlay counts for a lot when it covers at least OVERLAY_MIN_SHARE of the lot.
OVERLAYS_EXCLUDE = {   # strong constraint: lot is left out by default
    "HO": "Heritage", "LSIO": "Land subject to inundation", "SBO": "Special building (flooding)",
    "FO": "Floodway", "RFO": "Rural floodway", "EAO": "Environmental audit (possible contamination)",
    "PAO": "Public acquisition", "BMO": "Bushfire management",
}
OVERLAYS_REVIEW = {    # shapes what can be built: lot stays in but is flagged for planner review
    "NCO": "Neighbourhood character", "DDO": "Design and development", "DPO": "Development plan",
    "VPO": "Vegetation protection", "ESO": "Environmental significance", "SLO": "Significant landscape",
    "SCO": "Specific controls", "EMO": "Erosion management", "MAEO": "Melbourne Airport environs",
    "AEO": "Airport environs", "IPO": "Incorporated plan", "PO": "Parking", "SRO": "State resource",
}
OVERLAYS_IGNORE = {    # funding mechanisms, not built-form constraints
    "DCPO": "Development contributions plan", "ICO": "Infrastructure contributions",
}
OVERLAY_MIN_SHARE = 0.05   # 5% of the lot
# Overlay codes not listed above are treated as "review" so nothing unknown is silently ignored.

# Default scenario values (all adjustable in the app)
DEFAULT_RADIUS_M = 800
DEFAULT_MIN_LOT_M2 = 500
DEFAULT_MAX_COVERAGE = 0.30
DEFAULT_DWELLINGS_PER_HA = 80  # roughly townhouses / 3-storey walk-ups
DEFAULT_MIN_WIDTH_M = 12       # narrow lots rarely fit more than a dual occupancy

# --- Data freshness ------------------------------------------------------------
STATUS_FILE = DATA_DIR / "status.json"
# Re-download a source when its file is older than this (used by the scheduler)
REFRESH_MAX_AGE_DAYS = int(os.environ.get("REFRESH_MAX_AGE_DAYS", "7"))
# Warn when more than this share of candidate-zone lots has no building recorded at all
FOOTPRINT_GAP_WARN = 0.10

# --- Satellite imagery (Phase 2) ---------------------------------------------
# Primary: Digital Earth Australia (Geoscience Australia) Sentinel-2 Analysis Ready
# Data - free, no account, CC BY 4.0, new pass over Melbourne every few days.
SAT_PROVIDERS = {
    "dea": {
        "stac": "https://explorer.dea.ga.gov.au/stac",
        "collections": ["ga_s2cm_ard_3", "ga_s2bm_ard_3", "ga_s2am_ard_3"],
        "bands": {"red": "nbart_red", "green": "nbart_green", "blue": "nbart_blue",
                  "nir": "nbart_nir_1", "swir": "nbart_swir_2", "mask": "oa_fmask"},
        "clear_values": [1, 5],          # fmask: 1 = clear land, 5 = water
        "cloud_property": "eo:cloud_cover",
        "scale": 1 / 10000,
    },
    # Fallback: Element 84 Earth Search (AWS open data)
    "earthsearch": {
        "stac": "https://earth-search.aws.element84.com/v1",
        "collections": ["sentinel-2-c1-l2a"],
        "bands": {"red": "red", "green": "green", "blue": "blue",
                  "nir": "nir", "swir": "swir16", "mask": "scl"},
        "clear_values": [4, 5, 6, 7],    # SCL: vegetation, bare soil, water, unclassified
        "cloud_property": "eo:cloud_cover",
        "scale": 1 / 10000,
    },
}
# Lot statistics are computed on a 2.5 m grid. This adds NO spatial detail - the imagery is still 10 m.
# It only lets each lot's boundary cut through 10 m pixels so a lot gets an area-weighted share of the
# pixels it partly covers, instead of all-or-nothing. Readings remain lot-level averages.
SAT_RES_M = 2.5
SAT_MAX_CLOUD = 20         # % scene cloud cover allowed when searching
SAT_LOOKBACK_DAYS = 60     # search window for the "current" scene
SAT_BASELINE_YEARS = 1     # compare against the same season this many years earlier
NDVI_GREEN = 0.40          # pixel counts as vegetated above this NDVI
CHANGE_NDVI_DROP = 0.15    # lot-mean NDVI drop that suggests clearing / construction
CHANGE_NDBI_RISE = 0.05    # lot-mean built-up index rise that suggests construction

# --- Walking network (Phase 2) -----------------------------------------------
WALK_SPEED_M_PER_MIN = 80  # ~4.8 km/h
WALK_MAX_M = 2000          # network search cutoff; lots further than this walk are 'no route within 2 km'

# --- ABS Census 2021 (Phase 2) -----------------------------------------------
ABS_SA1_BOUNDARIES_URL = ("https://www.abs.gov.au/statistics/standards/australian-statistical-geography-standard-asgs-edition-3/"
                          "jul2021-jun2026/access-and-downloads/digital-boundary-files/SA1_2021_AUST_SHP_GDA2020.zip")
ABS_GCP_URL = "https://www.abs.gov.au/census/find-census-data/datapacks/download/2021_GCP_SA1_for_VIC_short-header.zip"

# --- Infrastructure demand assumptions (Phase 2) ------------------------------
# PLACEHOLDERS for a pilot. Replace with figures from the relevant water corporation,
# distribution business and Department of Education before presenting results.
INFRA_ASSUMPTIONS = {
    "persons_per_new_dwelling": None,      # None = use the Census average for each lot's SA1
    "water_l_per_person_day": 160.0,
    "wastewater_l_per_person_day": 150.0,
    "peak_kva_per_dwelling": 3.5,
    "primary_students_per_dwelling": 0.15,
    "secondary_students_per_dwelling": 0.10,
}

# --- Everyday needs and emergency services --------------------------------------
# "daily" categories are measured as a WALK along streets and count towards the everyday-services
# score (inspired by Plan Melbourne's 20-minute neighbourhood: daily needs within ~800 m).
# "emergency" categories are measured as a STRAIGHT LINE - what matters is how close a crew or
# emergency department is, not whether a resident can walk there.
AMENITIES = {
    "supermarket": {"label": "Supermarket", "group": "shops", "kind": "daily",
                    "overture": ["supermarket", "grocery_store", "grocery"]},
    "market":      {"label": "Local market", "group": "shops", "kind": "extra",
                    "overture": ["farmers_market", "public_market", "market"]},
    "pharmacy":    {"label": "Pharmacy", "group": "health", "kind": "daily",
                    "overture": ["pharmacy", "drugstore", "chemist"]},
    "gp":          {"label": "GP or medical clinic", "group": "health", "kind": "daily",
                    "overture": ["doctor", "general_practitioner", "medical_center", "family_practice", "medical_clinic", "clinic"]},
    "school":      {"label": "School", "group": "education", "kind": "daily",
                    "overture": ["school", "elementary_school", "primary_school", "high_school", "middle_school"]},
    "childcare":   {"label": "Childcare or kindergarten", "group": "education", "kind": "daily",
                    "overture": ["child_care_and_day_care", "preschool", "kindergarten"]},
    "park":        {"label": "Park", "group": "parks", "kind": "daily",
                    "overture": ["park", "playground"]},
    "hospital":    {"label": "Hospital", "group": "emergency", "kind": "emergency",
                    "overture": ["hospital", "emergency_room"]},
    "police":      {"label": "Police station", "group": "emergency", "kind": "emergency",
                    "overture": ["police_department", "police_station", "police"]},
    "fire":        {"label": "Fire station", "group": "emergency", "kind": "emergency",
                    "overture": ["fire_department", "fire_station"]},
    "ambulance":   {"label": "Ambulance station", "group": "emergency", "kind": "emergency",
                    "overture": ["ambulance_and_ems_services", "ambulance_station", "emergency_service"]},
}
AMENITY_GROUPS = {"shops": "Shops and markets", "health": "Pharmacies and GPs",
                  "education": "Schools and childcare", "parks": "Parks", "emergency": "Emergency services"}
AMENITY_PAD_DEG = 0.05            # search ~5 km beyond the study area so edge lots find their nearest
DEFAULT_SERVICES_WALK_M = 800     # 20-minute neighbourhood: ~10 minutes' walk each way
SERVICES_WALK_MAX_M = 3000        # walking search cutoff for everyday needs
