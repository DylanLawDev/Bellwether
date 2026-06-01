"""Dashboard — coverage time series + anomaly flags for tracked symbols."""

import altair as alt
import streamlit as st

from bellweather.web import analysis, data

st.title("📈 Dashboard")
st.caption("Coverage over time for tracked symbols, with simple anomaly flags.")

symbols = data.get_tracked_symbols()
keys = symbols["key"].tolist()

selected = st.multiselect(
    "Tracked symbols",
    options=keys,
    default=keys[:3],
    help="Each symbol is a coverage time series keyed `tag_type:raw_value`.",
)
sigma = st.slider("Anomaly sensitivity (σ above mean)", 2.0, 5.0, 3.0, 0.5)

if not selected:
    st.info("Select at least one tracked symbol.")
    st.stop()

obs = data.get_observations(selected)
if obs.empty:
    st.warning("No observations for the selected symbols.")
    st.stop()

# Flag anomalies per symbol.
obs = obs.copy()
obs["anomaly"] = False
for key in selected:
    mask = obs["key"] == key
    obs.loc[mask, "anomaly"] = analysis.flag_anomalies(obs.loc[mask, "value"], sigma).values

line = (
    alt.Chart(obs)
    .mark_line()
    .encode(
        x=alt.X("ts_bucket:T", title="time"),
        y=alt.Y("value:Q", title="coverage"),
        color=alt.Color("key:N", title="symbol"),
    )
)
points = (
    alt.Chart(obs[obs["anomaly"]])
    .mark_point(size=80, filled=True, color="red")
    .encode(x="ts_bucket:T", y="value:Q", tooltip=["key", "ts_bucket", "value"])
)
st.altair_chart((line + points).properties(height=380), width="stretch")

left, right = st.columns(2)
with left:
    st.subheader("Top movers")
    st.dataframe(
        analysis.top_movers(symbols)[["key", "latest_value", "total_samples"]],
        hide_index=True,
        width="stretch",
    )
with right:
    st.subheader("Flagged buckets")
    flagged = obs[obs["anomaly"]][["key", "ts_bucket", "value"]]
    if flagged.empty:
        st.write("No anomalies at the current sensitivity.")
    else:
        st.dataframe(flagged, hide_index=True, width="stretch")
