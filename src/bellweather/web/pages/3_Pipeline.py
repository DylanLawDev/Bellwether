"""Pipeline — work-queue health and recent worker runs."""

import streamlit as st

from bellweather.web import data

st.title("⚙️ Pipeline Status")
st.caption("Work-queue state, recent worker runs, and ingestion throughput.")

queue = data.get_queue_stats()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Pending", queue["pending"])
c2.metric("Leased", queue["leased"])
c3.metric("Done", queue["done"])
c4.metric("Failed", queue["failed"], delta_color="inverse")

if queue["failed"] > 0:
    st.warning(
        f"{queue['failed']} job(s) dead-lettered to `failed` (exceeded max_attempts).",
        icon="⚠️",
    )

st.subheader("Recent worker runs")
runs = data.get_worker_runs()
st.dataframe(runs, hide_index=True, width="stretch")

st.subheader("Ingestion rate (last 48h)")
rate = data.get_ingestion_rate()
st.bar_chart(rate.set_index("hour")["records"], height=240)
