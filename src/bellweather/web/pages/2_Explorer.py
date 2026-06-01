"""Explorer — query raw records, tags, and observations."""

import streamlit as st

from bellweather.web import data

st.title("🔎 Data Explorer")
st.caption("Filter and inspect the bronze/silver/gold rows behind the signals.")

records_tab, tags_tab, obs_tab = st.tabs(["Raw records", "Tags", "Observations"])

PAGE_SIZE = 50

with records_tab:
    c1, c2, c3 = st.columns(3)
    status = c1.selectbox("Status", ["(any)", "received", "processed", "unroutable", "failed"])
    content_type = c2.selectbox("Content type", ["(any)", "gdelt.gkg"])
    search = c3.text_input("Idempotency key contains")
    page = st.number_input("Page", min_value=1, value=1, step=1, key="rec_page")
    df = data.query_raw_records(
        status=None if status == "(any)" else status,
        content_type=None if content_type == "(any)" else content_type,
        search=search or None,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    st.caption(f"{len(df)} rows (page {page}, {PAGE_SIZE}/page)")
    st.dataframe(df, hide_index=True, width="stretch")
    if not df.empty:
        with st.expander("Row detail"):
            row = st.selectbox("Record id", df["id"].tolist())
            st.json(df[df["id"] == row].iloc[0].to_dict(), expanded=True)

with tags_tab:
    c1, c2 = st.columns(2)
    tag_type = c1.selectbox("Tag type", ["(any)", "theme", "person", "org", "location", "tone"])
    tsearch = c2.text_input("Value contains", key="tag_search")
    tpage = st.number_input("Page", min_value=1, value=1, step=1, key="tag_page")
    tdf = data.query_tags(
        tag_type=None if tag_type == "(any)" else tag_type,
        search=tsearch or None,
        limit=PAGE_SIZE,
        offset=(tpage - 1) * PAGE_SIZE,
    )
    st.caption(f"{len(tdf)} rows (page {tpage}, {PAGE_SIZE}/page)")
    st.dataframe(tdf, hide_index=True, width="stretch")

with obs_tab:
    symbols = data.get_tracked_symbols()
    chosen = st.multiselect("Symbols", symbols["key"].tolist(), default=symbols["key"].tolist()[:2])
    odf = data.get_observations(chosen) if chosen else data.get_observations([])
    st.caption(f"{len(odf)} observation rows")
    st.dataframe(odf, hide_index=True, width="stretch")
