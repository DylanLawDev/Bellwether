"""Scrape specs — master/detail control plane.

Select a spec (or "➕ New spec…"), edit it in place, and preview any of its sites
(dry-run; commits nothing). Reads/writes only through bellweather.web.data (mock
or live). Scheduling is source-agnostic and lives on the Schedules page — bind
template "scrape" with params {"spec": <name>} there.
"""

import json

import streamlit as st

from bellweather.web import data
from bellweather.web.pages import _scrape_form as form

NEW = "➕ New spec…"
EXAMPLE_SCHEMA = (
    '{\n  "type": "object",\n  "properties": {\n'
    '    "title": {"type": "string"},\n    "price": {"type": "number"}\n  }\n}'
)
EXAMPLE_BINDING = (
    '{\n  "symbol_key": "scrape:demo:{title}",\n  "symbol_kind": "scraped-metric",\n'
    '  "value": "$.price",\n  "ts": "fetched_at",\n  "unit": "usd",\n  "tags": []\n}'
)

st.title("Scrape specs")
st.caption(
    "Declare {sites, output schema, binding} once; edit in place and preview per-site. "
    "Schedule from the Schedules page with the 'scrape' template."
)

specs = data.get_scrape_specs()
names = list(specs["name"]) if not specs.empty else []
choice = st.selectbox("Spec", [NEW, *names])
is_new = choice == NEW
spec = None if is_new else data.get_scrape_spec(choice)
# The selected spec's name, used by the Preview/Delete controls below.
selected_name = "" if is_new else spec["name"]

st.caption('→ Schedule this spec on the **Schedules** page (template "scrape").')

edit_tab, preview_tab = st.tabs(["Edit", "Preview"])

with edit_tab:
    adapters = data.get_fetch_adapter_choices()
    if is_new:
        defaults = {
            "description": "",
            "sites": "https://example.com/",
            "schema": EXAMPLE_SCHEMA,
            "binding": EXAMPLE_BINDING,
            "model": "",
            "enabled": True,
        }
        adapter_options, adapter_idx = adapters, 0
    else:
        defaults = {
            "description": spec.get("description") or "",
            "sites": "\n".join(spec.get("sites") or []),
            "schema": json.dumps(spec["output_schema"], indent=2),
            "binding": json.dumps(spec["binding"], indent=2),
            "model": spec.get("llm_model") or "",
            "enabled": bool(spec["enabled"]),
        }
        # Keep the spec's current adapter selectable even if the registry no
        # longer lists it, so the selectbox never errors on a stale value.
        adapter_options = sorted(set(adapters) | {spec["fetch_adapter"]})
        adapter_idx = adapter_options.index(spec["fetch_adapter"])

    with st.form("spec_form"):
        if is_new:
            name = st.text_input("Spec name", value="my-spec")
        else:
            st.text_input("Spec name", value=selected_name, disabled=True)
            name = selected_name
        description = st.text_input("Description", value=defaults["description"])
        sites_raw = st.text_area("Sites (one URL per line)", value=defaults["sites"])
        output_schema_raw = st.text_area("Output schema (JSON Schema)", value=defaults["schema"])
        binding_raw = st.text_area("Binding (JSON)", value=defaults["binding"])
        c1, c2 = st.columns(2)
        fetch_adapter = c1.selectbox("Fetch adapter", adapter_options, index=adapter_idx)
        llm_model = c2.text_input("LLM model (blank = default)", value=defaults["model"])
        enabled = st.toggle("Enabled", value=defaults["enabled"])
        submitted = st.form_submit_button("Create spec" if is_new else "Save changes")

    if submitted:
        payload, errors = form.build_spec_payload(
            name=name,
            description=description,
            sites_raw=sites_raw,
            output_schema_raw=output_schema_raw,
            binding_raw=binding_raw,
            fetch_adapter=fetch_adapter,
            llm_model=llm_model,
            require_name=is_new,
        )
        if errors:
            for e in errors:
                st.error(e)
        elif is_new:
            sid = data.create_scrape_spec(
                payload["name"],
                payload["sites"],
                payload["output_schema"],
                payload["binding"],
                description=payload["description"],
                fetch_adapter=payload["fetch_adapter"],
                llm_model=payload["llm_model"],
            )
            # create defaults to enabled=True; honor an unchecked toggle via PATCH.
            if not enabled:
                data.update_scrape_spec(payload["name"], enabled=False)
            st.success(f"Created scrape spec #{sid}.")
            st.rerun()
        else:
            data.update_scrape_spec(
                selected_name,
                description=payload["description"],
                sites=payload["sites"],
                output_schema=payload["output_schema"],
                binding=payload["binding"],
                fetch_adapter=payload["fetch_adapter"],
                llm_model=payload["llm_model"],
                enabled=enabled,
            )
            st.success("Saved changes.")
            st.rerun()

    if not is_new and st.button("Delete spec"):
        data.delete_scrape_spec(selected_name)
        st.rerun()

with preview_tab:
    if is_new:
        st.info("Create the spec first, then preview its sites here.")
    else:
        sites = spec.get("sites") or []
        if not sites:
            st.info("This spec has no sites to preview.")
        else:
            url = st.selectbox("Preview which site?", sites)
            if st.button("Run preview (dry-run)"):
                try:
                    with st.spinner("Fetching + extracting (commits nothing)…"):
                        out = data.preview_scrape_spec(selected_name, url=url)
                except Exception as exc:  # noqa: BLE001 — surface any backend error to the operator
                    st.error(f"Preview failed: {exc}")
                else:
                    st.success(
                        f"Would emit {len(out['sample'])} sample point(s) across "
                        f"{len(out['symbols'])} symbol(s) and {len(out['tags'])} tag(s)."
                    )
                    st.markdown("**Extracted JSON**")
                    st.json(out["extracted"])
                    st.markdown("**Sample observations**")
                    st.dataframe(out["sample"], hide_index=True)
                    st.markdown("**Tags**")
                    st.dataframe(out["tags"], hide_index=True)
