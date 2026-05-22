"""
app.py — Chester Buses Streamlit app.
"""

import datetime
import math
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from streamlit_geolocation import streamlit_geolocation


DB_PATH = Path("data/chester.db")
TZ = ZoneInfo("Europe/London")
WEEKDAY_COLS = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]


# --- Helpers ----------------------------------------------------------------

def format_gtfs_time(s):
    """Convert HH:MM:SS GTFS time to display format. Handles >24h times."""
    if s is None or (isinstance(s, float) and pd.isna(s)) or s == "":
        return "--"
    h, m, _ = s.split(":")
    h = int(h)
    if h >= 24:
        return f"{h - 24:02d}:{m} (next day)"
    return f"{h:02d}:{m}"


def minutes_until(dep_time_str, now):
    """Format minutes from `now` until a GTFS HH:MM:SS departure time."""
    h, m, _ = dep_time_str.split(":")
    h, m = int(h), int(m)
    days_offset = 0
    if h >= 24:
        days_offset = 1
        h -= 24
    dep_dt = datetime.datetime.combine(
        now.date() + datetime.timedelta(days=days_offset),
        datetime.time(hour=h, minute=m),
        tzinfo=TZ,
    )
    delta_mins = int((dep_dt - now).total_seconds() // 60)
    if delta_mins < 1:
        return "due"
    if delta_mins < 60:
        return f"{delta_mins} min"
    hrs, rem = divmod(delta_mins, 60)
    return f"{hrs}h {rem:02d}m" if rem else f"{hrs}h"


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in metres."""
    r = 6_371_000  # Earth radius in metres
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


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


@st.cache_data(ttl=3600)
def load_stops_with_coords():
    """One row per unique stop name, with bay stop_ids and an average coordinate."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    try:
        stops = pd.read_sql_query(
            "SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops", conn
        )
    finally:
        conn.close()
    grouped = (
        stops.groupby("stop_name")
        .agg(
            stop_id=("stop_id", list),
            stop_lat=("stop_lat", "mean"),
            stop_lon=("stop_lon", "mean"),
        )
        .reset_index()
    )
    return grouped


def get_next_departures(stop_ids, now=None, limit=15):
    """Next N departures from any of the given stop_ids, after `now`.

    Returns trip_id and boarding_seq alongside the display fields so the
    caller can look up the onward stops for a chosen departure.
    """
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
            st.trip_id,
            st.stop_sequence AS boarding_seq,
            st.departure_time,
            r.route_short_name,
            COALESCE(
                NULLIF(t.terminus, ''),
                NULLIF(t.trip_headsign, ''),
                r.route_long_name
            ) AS destination,
            a.agency_name AS operator
        FROM stop_times st
        JOIN trips  t  ON st.trip_id  = t.trip_id
        JOIN routes r  ON t.route_id  = r.route_id
        JOIN agency a  ON r.agency_id = a.agency_id
        JOIN stops  bs ON st.stop_id  = bs.stop_id
        WHERE st.stop_id IN ({placeholders})
          AND t.service_id IN (SELECT service_id FROM services_today)
          AND st.departure_time >= ?
          AND (
              -- The trip continues to a later stop we have stored...
              EXISTS (
                  SELECT 1 FROM stop_times sl
                  WHERE sl.trip_id = st.trip_id
                    AND sl.stop_sequence > st.stop_sequence
              )
              -- ...or it continues out of our coverage area (so this stop
              -- isn't the real terminus). Excludes circular loop-backs and
              -- genuine end-of-line arrivals, which aren't boardable.
              OR COALESCE(t.terminus, '') <> bs.stop_name
          )
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


def get_onward_stops(trip_id, from_sequence):
    """Stops a given trip visits after `from_sequence`, with arrival times.

    Limited to stops stored in our database (the Chester area). For routes
    that continue beyond the area, this list is truncated at the boundary.
    """
    sql = """
        SELECT
            st.stop_sequence,
            COALESCE(NULLIF(st.arrival_time, ''), st.departure_time) AS arrival_time,
            s.stop_name
        FROM stop_times st
        JOIN stops s ON st.stop_id = s.stop_id
        WHERE st.trip_id = ? AND st.stop_sequence > ?
        ORDER BY st.stop_sequence
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        return pd.read_sql_query(sql, conn, params=[trip_id, int(from_sequence)])
    finally:
        conn.close()


def get_stop_points(stop_ids):
    """Lat/lon for each bay of a stop, for mapping."""
    placeholders = ",".join(["?"] * len(stop_ids))
    sql = f"""
        SELECT stop_lat AS lat, stop_lon AS lon
        FROM stops
        WHERE stop_id IN ({placeholders})
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        return pd.read_sql_query(sql, conn, params=list(stop_ids))
    finally:
        conn.close()


def build_map_points(stop_points, user_lat=None, user_lon=None):
    """Combine stop bay points (red) with the user's location (blue) for st.map.

    Colours carry a 50% alpha channel (the trailing `80`).
    """
    pts = stop_points.copy()
    pts["color"] = "#d6272880"  # red @ 50% = the stop
    if user_lat is not None and user_lon is not None:
        user_row = pd.DataFrame(
            {"lat": [user_lat], "lon": [user_lon], "color": ["#1f77b480"]}  # blue @ 50% = you
        )
        pts = pd.concat([pts, user_row], ignore_index=True)
    return pts


def show_departures(selected_name, stop_ids):
    """Render the departures for a chosen stop, each expandable to onward stops."""
    bay_count = len(stop_ids)
    now = datetime.datetime.now(TZ)
    st.subheader(selected_name)
    st.caption(
        f"As of {now.strftime('%H:%M')} on {now.strftime('%A %d %B %Y')} — "
        f"covering {bay_count} bay{'s' if bay_count != 1 else ''}"
    )

    with st.expander("Show this stop on a map"):
        points = get_stop_points(stop_ids)
        if points.empty:
            st.write("No location data for this stop.")
        else:
            map_points = build_map_points(
                points,
                st.session_state.get("user_lat"),
                st.session_state.get("user_lon"),
            )
            has_user = len(map_points) > len(points)
            if has_user:
                # Two markers — let the map auto-fit so both are visible.
                st.map(map_points, color="color", size=80)
                st.caption("Blue dot = you · red dot = the stop")
            else:
                st.map(map_points, zoom=15, color="color", size=80)

    departures = get_next_departures(stop_ids, now=now, limit=30)
    if departures.empty:
        st.info("No more departures today from this stop.")
        return

    # Same trip across multiple bays produces duplicate rows; collapse them.
    departures = departures.drop_duplicates(
        subset=["departure_time", "route_short_name", "destination"], keep="first"
    ).head(15).reset_index(drop=True)

    st.caption("Tap a departure to see its onward stops.")
    for _, dep in departures.iterrows():
        route = dep["route_short_name"] if dep["route_short_name"] else "?"
        clock = format_gtfs_time(dep["departure_time"])
        mins = minutes_until(dep["departure_time"], now)
        dest = dep["destination"] or ""
        header = f"{mins}  ·  {clock}  ·  Route {route} → {dest}"

        with st.expander(header):
            onward = get_onward_stops(dep["trip_id"], dep["boarding_seq"])
            st.caption(f"Operated by {dep['operator']}")
            if onward.empty:
                st.write("This stop is the end of the line for this service.")
            else:
                onward_display = pd.DataFrame({
                    "Arrives": onward["arrival_time"].apply(format_gtfs_time),
                    "Stop": onward["stop_name"],
                })
                st.dataframe(
                    onward_display, use_container_width=True, hide_index=True
                )
                last_shown = onward["stop_name"].iloc[-1]
                if dest and last_shown != dest:
                    st.caption(
                        f"Continues towards {dest} — stops beyond the Chester "
                        f"area aren't shown."
                    )


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

# Track the chosen stop across reruns.
if "chosen_stop" not in st.session_state:
    st.session_state.chosen_stop = None

# --- Location section --------------------------------------------------------
st.write("**Find stops near you**")
st.caption("Tap the location pin, allow access, and we'll list the closest stops.")
loc = streamlit_geolocation()

if loc and loc.get("latitude") and loc.get("longitude"):
    st.session_state.user_lat = loc["latitude"]
    st.session_state.user_lon = loc["longitude"]
    coords = load_stops_with_coords().copy()
    coords["dist_m"] = coords.apply(
        lambda row: haversine_m(
            loc["latitude"], loc["longitude"], row["stop_lat"], row["stop_lon"]
        ),
        axis=1,
    )
    nearest = coords.nsmallest(6, "dist_m")
    st.caption("Closest stops to you:")
    for _, row in nearest.iterrows():
        dist = int(row["dist_m"])
        label = f"{row['stop_name']} — {dist} m away"
        if st.button(label, key=f"near_{row['stop_name']}"):
            st.session_state.chosen_stop = row["stop_name"]

st.divider()

# --- Search section ----------------------------------------------------------
st.write("**Or search by name**")
selected_name = st.selectbox(
    "Search for a stop",
    options=stops["stop_name"].tolist(),
    index=None,
    placeholder="Type to search (e.g. Chester Bus Interchange)...",
    label_visibility="collapsed",
)
if selected_name:
    st.session_state.chosen_stop = selected_name

# --- Departures --------------------------------------------------------------
st.divider()
chosen = st.session_state.chosen_stop
if chosen:
    match = stops[stops["stop_name"] == chosen]
    if not match.empty:
        show_departures(chosen, match.iloc[0]["stop_id"])
else:
    st.info("Pick a stop above to see departures.")
