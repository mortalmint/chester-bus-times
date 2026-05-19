"""
app.py — Chester Buses Streamlit app.
"""

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st


DB_PATH = Path("data/chester.db")


@st.cache_data(ttl=3600)
def load_stops():
    """Load all Chester-area stops from the SQLite database."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    try:
        return pd.read_sql_query(
            """
            SELECT stop_id, stop_name, stop_lat, stop_lon
            FROM stops
            ORDER BY stop_name
            """,
            conn,
        )
    finally:
        conn.close()


st.set_page_config(
    page_title="Chester Buses",
    page_icon="🚌",
    layout="centered",
)

st.title("Chester Buses")
st.caption("Find your next bus in Chester. Data from BODS (DfT).")

stops = load_stops()

if stops.empty:
    st.warning(
        "No data loaded yet. The GitHub Actions workflow needs to run "
        "to populate the database."
    )
else:
    st.success(f"Loaded {len(stops):,} Chester-area bus stops.")

    query = st.text_input(
        "Search for a stop",
        placeholder="Try: Chester, or a street name",
    )

    if query:
        results = stops[stops["stop_name"].str.contains(query, case=False, na=False)]
        st.write(f"**{len(results)} match{'es' if len(results) != 1 else ''}**")
        st.dataframe(
            results[["stop_name", "stop_id"]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Type something above to search, or browse all stops below.")
        st.dataframe(
            stops[["stop_name", "stop_id"]],
            use_container_width=True,
            hide_index=True,
        )
