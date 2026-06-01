"""Settings — configuration view (read-only in the prototype)."""

import streamlit as st

from bellweather.web import data

st.title("🛠️ Settings")
st.caption("Configuration mirrors `bellweather.config.Settings`. Prototype: Save is a no-op.")

st.info(
    "In this prototype, **Save** echoes the would-be payload without persisting. "
    "Wiring real config writes is later work.",
    icon="🧪",
)

settings = data.get_settings_view()

with st.form("settings_form"):
    edited = {}
    for field in settings:
        edited[field["key"]] = st.text_input(
            field["key"], value=str(field["value"]), help=field["note"]
        )
    submitted = st.form_submit_button("Save")

if submitted:
    st.success("Would save (no-op in prototype):")
    st.json(edited)
