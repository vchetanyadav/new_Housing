#!/bin/sh
# Data pipeline runner. Usage: sh run_pipeline.sh [verify|full|fresh|refresh|services|lotplan|addresses|process|satellite|demo|test|schedule]
set -e
DATA="${DATA_DIR:-../data}"

clear_demo() {
  if [ -f "$DATA/raw/.demo" ]; then
    echo "Removing demo data before downloading real data..."
    rm -rf "$DATA/raw" "$DATA/processed"
  fi
}

case "${1:-full}" in
  verify)
    # Check every source and its structure (about a minute, no large downloads)
    python -m src.verify ;;
  full)
    clear_demo
    python -m src.ingest
    python -m src.satellite || echo "WARNING: satellite step failed; continuing without it"
    python -m src.process ;;
  fresh)
    # Re-download every map layer from scratch (keeps the large ABS Census downloads), then rebuild.
    clear_demo
    rm -f "$DATA"/raw/parcels.parquet "$DATA"/raw/zones.parquet "$DATA"/raw/overlays.parquet \
          "$DATA"/raw/stations.parquet "$DATA"/raw/buildings.parquet "$DATA"/raw/amenities.parquet \
          "$DATA"/raw/walk_network.graphml
    python -m src.ingest
    python -m src.satellite || echo "WARNING: satellite step failed; continuing without it"
    python -m src.process ;;
  refresh)
    # Re-download any source older than REFRESH_MAX_AGE_DAYS, refresh satellite, rebuild.
    # Failed downloads keep the previous copy and are flagged in the app.
    python -m src.ingest --max-age-days "${REFRESH_MAX_AGE_DAYS:-7}"
    python -m src.satellite || echo "WARNING: satellite refresh failed; keeping previous imagery"
    python -m src.process ;;
  services)
    # Re-download shops, health, schools, parks and emergency services, then rebuild the lot table
    rm -f "$DATA"/raw/amenities.parquet
    python -m src.ingest
    python -m src.process ;;
  lotplan)
    # Re-download only the lot-and-plan numbers, then rebuild the lot table
    rm -f "$DATA"/raw/parcel_detail.parquet
    python -m src.ingest
    python -m src.process ;;
  addresses)
    # (Re-)download lot-and-plan numbers and street addresses, then rebuild the lot table
    rm -f "$DATA"/raw/parcel_detail.parquet "$DATA"/raw/addresses.parquet
    python -m src.ingest
    python -m src.process ;;
  process)
    # Rebuild the lot table from data already downloaded (e.g. retry the walking network)
    python -m src.process ;;
  satellite)
    python -m src.satellite
    python -m src.process ;;
  demo)
    rm -rf "$DATA/raw" "$DATA/processed"
    python -m tests.make_fake_data
    touch "$DATA/raw/.demo"
    python -m src.process ;;
  test)
    # Uses its own scratch folder so it never touches the real data the site is serving.
    export DATA_DIR="${TMPDIR:-/tmp}/hof-test-data"
    rm -rf "$DATA_DIR"
    python -m tests.make_fake_data
    python -m src.process
    python -m pytest -q -p no:warnings ;;
  schedule)
    echo "Refreshing all sources every ${REFRESH_SECONDS:-86400}s (re-downloads anything older than ${REFRESH_MAX_AGE_DAYS:-7} days)"
    while true; do
      sh "$0" refresh || echo "refresh cycle failed; will retry next cycle"
      sleep "${REFRESH_SECONDS:-86400}"
    done ;;
  *) echo "Unknown mode: $1 (use verify, full, fresh, refresh, services, lotplan, addresses, process, satellite, demo, test or schedule)"; exit 1 ;;
esac
