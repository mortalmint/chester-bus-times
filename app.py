"""
app.py — Chester Buses Streamlit app.
"""

import datetime
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st


DB_PATH = Path("data/chester.db")
TZ = ZoneInfo("Europe/London")
WEEKDAY_COLS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]


# --- Data access -------------------------------------------------------------

@st.cache_data(ttl=3600)
def load_stops_grouped():
    """Stops grouped by name. One row per unique name, with all bay stop_ids."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    try:
        stops = pd.read_sql_query("SELECT stop_id, stop_name FROM stops", conn)
    finally:
        conn.close()
    grouped = (
        stops.groupby("stop_name")["stop_id"]
        .apply(list)
        .reset_index()
        .sort_values("stop_name")
        .reset_index(drop=True)
    )
    return grouped


def get_next_departures(stop_ids, now=None, limit=15):
    """Next N departures from any of the given stop_ids, after `now`."""
    if now is None:
        now = datetime.datetime.now(TZ)

    today = now.date()
    weekday_col = WEEKDAY_COLS[today.weekday()]
    today_int = int(today.strftime("%Y%m%d"))
    now_time_str = now.strftime("%H:%M:%S")

    placeholders = ",".join(["?"] * len(stop_ids))

    sql = f"""
        WITH services_today AS (
            SELECT service_id FROM calendar
            WHERE {weekday_col} = 1
              AND start_date <= ?
              AND end_date   >= ?
              AND service_id NOT IN (
                  SELECT service_id FROM calendar_dates
                  WHERE date = ? AND exception_type = 2
              )
            UNION
            SELECT service_id FROM calendar_dates
            WHERE date = ? AND exception_type = 1
        )
        SELECT
            st.departure_time,
            r.route_short_name,
            COALESCE(
                NULLIF(t.terminus, ''),
                NULLIF(t.trip_headsign, ''),
                r.route_long_name
            ) AS destination,
            a.agency_name AS operator
        FROM stop_times st
        JOIN trips  t ON st.trip_id  = t.trip_id
        JOIN routes r ON t.route_id  = r.route_id
        JOIN agency a ON r.agency_id = a.agency_id
        WHERE st.stop_id IN ({placeholders})
          AND t.service_id IN (SELECT service_id FROM services_today)
          AND st.departure_time >= ?
        ORDER BY st.departure_time
        LIMIT ?
    """
    params = (
        [today_int, today_int, today_int, today_int]
        + list(stop_ids)
        + [now_time_str, limit]
    )

    conn = sqlite3.connect(DB_PATH)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()

def format_gtfs_time(s):
    """Convert HH:MM:SS GTFS time to display format. Handles >24h times."""
    h, m, _ = s.split(":")
    h = int(h)
    if h >= 24:
        return f"{h - 24:02d}:{m} (next day)"
    return f"{h:02d}:{m}"


# --- UI ----------------------------------------------------------------------

st.set_page_config(
    page_title="Chester Buses",
    page_icon="🚌",
    layout="centered",
)

st.title("Chester Buses")
st.caption("Next departures from Chester-area bus stops. Data from BODS (DfT).")

stops = load_stops_grouped()

if stops.empty:
    st.warning("No data loaded.")
    st.stop()

st.caption(f"{len(stops):,} unique stops loaded.")

selected_name = st.selectbox(
    "Pick a stop",
    options=stops["stop_name"].tolist(),
    index=None,
    placeholder="Type to search (e.g. Chester Bus Interchange)...",
)

if selected_name:
    selected_row = stops[stops["stop_name"] == selected_name].iloc[0]
    stop_ids = selected_row["stop_id"]
    bay_count = len(stop_ids)

    now = datetime.datetime.now(TZ)
    st.subheader(selected_name)
    st.caption(
        f"As of {now.strftime('%H:%M')} on {now.strftime('%A %d %B %Y')} — "
        f"covering {bay_count} bay{'s' if bay_count != 1 else ''}"
    )

    departures = get_next_departures(stop_ids, now=now, limit=15)

    if departures.empty:
        st.info("No more departures today from this stop.")
    else:
        display = pd.DataFrame({
            "Time": departures["departure_time"].apply(format_gtfs_time),
            "Route": departures["route_short_name"],
            "Destination": departures["destination"],
            "Operator": departures["operator"],
        })
        st.dataframe(display, use_container_width=True, hide_index=True)
