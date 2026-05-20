"""
ingest.py — Download BODS North West GTFS, filter to Chester area, write SQLite.

Run manually (locally):
    python ingest.py

Run in CI:
    Triggered by .github/workflows/refresh-data.yml
"""

import os
import sqlite3
import zipfile
from pathlib import Path

import pandas as pd
import requests


# --- Configuration -----------------------------------------------------------

# BODS GTFS regional download endpoint.
# Auth: this endpoint appears to work without an API key, but we'll send one
# if BODS_API_KEY is set in the environment, just in case.
BODS_GTFS_URL = (
    "https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/north_west/"
)

# Chester-area bounding box. Generous; tighten later if results are noisy.
LAT_MIN, LAT_MAX = 53.05, 53.35
LON_MIN, LON_MAX = -3.15, -2.60

# Which GTFS route_types to include.
# 3 = bus. (0 = tram, 4 = ferry, 200 = coach, etc.) Add more here later if needed.
ALLOWED_ROUTE_TYPES = {3}

# Paths
DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "chester.db"
GTFS_ZIP_PATH = DATA_DIR / "north_west_gtfs.zip"


# --- Steps -------------------------------------------------------------------

def download_gtfs():
    """Download the BODS regional GTFS bundle to disk."""
    DATA_DIR.mkdir(exist_ok=True)
    params = {}
    api_key = os.environ.get("BODS_API_KEY")
    if api_key:
        params["api_key"] = api_key

    print(f"Downloading {BODS_GTFS_URL} ...")
    response = requests.get(BODS_GTFS_URL, params=params, stream=True, timeout=300)
    response.raise_for_status()

    bytes_written = 0
    with GTFS_ZIP_PATH.open("wb") as f:
        for chunk in response.iter_content(chunk_size=1_000_000):
            f.write(chunk)
            bytes_written += len(chunk)
    print(f"  Downloaded {bytes_written / 1_000_000:.1f}MB to {GTFS_ZIP_PATH}")


def read_gtfs(zip_handle, filename, **kwargs):
    """Open one CSV from inside the GTFS zip and return as DataFrame."""
    with zip_handle.open(filename) as f:
        return pd.read_csv(f, **kwargs)


def filter_to_chester():
    """Walk the GTFS files, keeping only Chester-area, bus-only data."""
    print("Filtering to Chester area...")
    with zipfile.ZipFile(GTFS_ZIP_PATH) as zf:
        # Step 1: stops within the bounding box.
        all_stops = read_gtfs(zf, "stops.txt")
        in_box = (
            all_stops["stop_lat"].between(LAT_MIN, LAT_MAX)
            & all_stops["stop_lon"].between(LON_MIN, LON_MAX)
        )
        chester_stops_df = all_stops.loc[in_box].copy()
        chester_stop_ids = set(chester_stops_df["stop_id"])
        print(f"  Stops in bounding box: {len(chester_stops_df)}")

        # Step 2: first pass through stop_times — find trips visiting Chester.
        chester_trip_ids = set()
        for chunk in pd.read_csv(
            zf.open("stop_times.txt"),
            chunksize=200_000,
            dtype={"stop_id": str, "trip_id": str},
        ):
            visiting = chunk[chunk["stop_id"].isin(chester_stop_ids)]["trip_id"]
            chester_trip_ids.update(visiting.unique())
        print(f"  Trips that visit Chester: {len(chester_trip_ids):,}")

        # Step 3: second pass — load ALL stop_times for those trips,
        # including out-of-area stops (we need the terminus, which may be
        # outside the bounding box, e.g. Liverpool, Wrexham).
        full_stop_times_chunks = []
        for chunk in pd.read_csv(
            zf.open("stop_times.txt"),
            chunksize=200_000,
            dtype={"stop_id": str, "trip_id": str},
        ):
            full_stop_times_chunks.append(chunk[chunk["trip_id"].isin(chester_trip_ids)])
        full_stop_times = pd.concat(full_stop_times_chunks, ignore_index=True)
        print(f"  Full stop-times for Chester trips: {len(full_stop_times):,}")

        # Step 4: compute terminus (final stop name) per trip.
        terminus = (
            full_stop_times.sort_values(["trip_id", "stop_sequence"])
            .groupby("trip_id")
            .tail(1)[["trip_id", "stop_id"]]
            .rename(columns={"stop_id": "terminus_stop_id"})
        )
        terminus = terminus.merge(
            all_stops[["stop_id", "stop_name"]].rename(
                columns={"stop_id": "terminus_stop_id", "stop_name": "terminus"}
            ),
            on="terminus_stop_id",
            how="left",
        )[["trip_id", "terminus"]]
        print(f"  Trip termini computed: {len(terminus):,}")

        # Step 5: load trips, attach terminus, filter to Chester trip_ids.
        trips = read_gtfs(zf, "trips.txt", dtype={"trip_id": str})
        trips = trips[trips["trip_id"].isin(chester_trip_ids)].copy()
        trips = trips.merge(terminus, on="trip_id", how="left")

        # Step 6: routes — filter to bus-only.
        chester_route_ids = set(trips["route_id"])
        routes = read_gtfs(zf, "routes.txt")
        routes = routes[
            routes["route_id"].isin(chester_route_ids)
            & routes["route_type"].isin(ALLOWED_ROUTE_TYPES)
        ].copy()
        valid_route_ids = set(routes["route_id"])
        print(f"  Routes (bus only): {len(routes)}")

        # Cascade the route filter back through trips.
        trips = trips[trips["route_id"].isin(valid_route_ids)]
        valid_trip_ids = set(trips["trip_id"])

        # Step 7: filter stop_times for storage — only Chester-area stops,
        # only trips we kept. The terminus is already on `trips`, so out-of-area
        # stops don't need to be stored.
        chester_stop_times = full_stop_times[
            full_stop_times["trip_id"].isin(valid_trip_ids)
            & full_stop_times["stop_id"].isin(chester_stop_ids)
        ]
        used_stop_ids = set(chester_stop_times["stop_id"])
        chester_stops_df = chester_stops_df[chester_stops_df["stop_id"].isin(used_stop_ids)]
        print(f"  Stops actually used by bus routes: {len(chester_stops_df)}")
        print(f"  Final stop-times: {len(chester_stop_times):,}")

        # Step 8: agency / calendar / calendar_dates — same as before.
        chester_agency_ids = set(routes["agency_id"])
        chester_service_ids = set(trips["service_id"])

        agency = read_gtfs(zf, "agency.txt")
        agency = agency[agency["agency_id"].isin(chester_agency_ids)]

        calendar = read_gtfs(zf, "calendar.txt")
        calendar = calendar[calendar["service_id"].isin(chester_service_ids)]

        try:
            calendar_dates = read_gtfs(zf, "calendar_dates.txt")
            calendar_dates = calendar_dates[
                calendar_dates["service_id"].isin(chester_service_ids)
            ]
        except KeyError:
            calendar_dates = pd.DataFrame()

        print(
            f"  Agencies: {len(agency)}  Calendars: {len(calendar)}  "
            f"Calendar exceptions: {len(calendar_dates)}"
        )

    return {
        "agency": agency,
        "stops": chester_stops_df,
        "routes": routes,
        "trips": trips,
        "stop_times": chester_stop_times,
        "calendar": calendar,
        "calendar_dates": calendar_dates,
    }


def write_sqlite(tables):
    """Write filtered tables to a fresh SQLite database with indexes."""
    DATA_DIR.mkdir(exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    print(f"Writing SQLite to {DB_PATH} ...")

    conn = sqlite3.connect(DB_PATH)
    for name, df in tables.items():
        df.to_sql(name, conn, index=False, if_exists="replace")
        print(f"  {name}: {len(df):,} rows")

    # Indexes for the queries we'll run from the app.
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_stop_times_stop_id  ON stop_times(stop_id);
        CREATE INDEX IF NOT EXISTS idx_stop_times_trip_id  ON stop_times(trip_id);
        CREATE INDEX IF NOT EXISTS idx_trips_route_id      ON trips(route_id);
        CREATE INDEX IF NOT EXISTS idx_trips_service_id    ON trips(service_id);
        CREATE INDEX IF NOT EXISTS idx_stops_stop_name     ON stops(stop_name);
        CREATE INDEX IF NOT EXISTS idx_calendar_service_id ON calendar(service_id);
        CREATE INDEX IF NOT EXISTS idx_caldates_service_id ON calendar_dates(service_id);
    """)
    conn.commit()
    conn.close()
    size_mb = DB_PATH.stat().st_size / 1_000_000
    print(f"  Database size on disk: {size_mb:.1f}MB")


def cleanup():
    """Remove the downloaded GTFS zip — we don't commit it to the repo."""
    if GTFS_ZIP_PATH.exists():
        GTFS_ZIP_PATH.unlink()


def main():
    download_gtfs()
    tables = filter_to_chester()
    write_sqlite(tables)
    cleanup()
    print("Done.")


if __name__ == "__main__":
    main()
