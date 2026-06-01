"""Bellwether web UI — entrypoint / overview.

Run with:  make ui   (or: uv run streamlit run src/bellweather/web/app.py)

A local operator/research surface running on MOCK data behind a swappable
data-access seam (bellweather.web.data). See
docs/specs/2026-05-31-ui-prototype-design.md.
"""

import streamlit as st

from bellweather.web import data

st.set_page_config(page_title="Bellwether", page_icon="🔔", layout="wide")

st.title("🔔 Bellwether")
st.caption("Observational signal pipeline — operator & research surface (prototype)")

if data.BACKEND == "mock":
    st.info(
        "Running on **mock data**. Screens read only from the `bellweather.web.data` seam, so "
        "pointing this at real read-endpoints later (`BELLWEATHER_UI_SOURCE=live`) "
        "needs no screen changes.",
        icon="🧪",
    )

symbols = data.get_tracked_symbols()
queue = data.get_queue_stats()
rate = data.get_ingestion_rate()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Tracked symbols", len(symbols))
c2.metric("Queue pending", queue["pending"])
c3.metric("Queue failed", queue["failed"])
c4.metric("Records ingested (48h)", int(rate["records"].sum()))

st.subheader("Ingestion rate (last 48h)")
st.line_chart(rate.set_index("hour")["records"], height=200)

st.subheader("Where to go")
st.markdown(
    "- **Dashboard** — coverage time series + anomaly flags for tracked symbols\n"
    "- **Explorer** — query raw records, tags, and observations\n"
    "- **Pipeline** — work-queue health and recent worker runs\n"
    "- **Settings** — configuration view\n\n"
    "Use the sidebar to navigate."
)
