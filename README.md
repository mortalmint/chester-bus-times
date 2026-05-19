
# Chester Buses

A mobile-friendly web app showing the next departures from Chester-area bus stops.

## Status

Phase 1: scaffolding. No data wired up yet.

## Data source

[Bus Open Data Service (BODS)](https://data.bus-data.dft.gov.uk/), run by the UK Department for Transport. Free to access, requires a registered API key.

## Stack

- **Streamlit** — Python app framework, deployed to Streamlit Community Cloud
- **SQLite** — local single-file database for parsed timetable data
- **GitHub Actions** — scheduled daily refresh of the BODS data

## Secrets

The BODS API key is stored in Streamlit Cloud's secrets manager, not in this repo. Never commit your API key.
