"""Extraction specs — parse-side control plane.

Select an extractor (or "➕ New extractor…"), edit how captures are parsed
(output schema + binding + model), choose which scrape sources it applies to
(the many-to-many edit surface), and test it against an existing raw capture —
no fetching happens here. Reads/writes only through bellweather.web.data (mock
or live). The future extraction playground (paste arbitrary HTML/markdown,
iterate, save labeled examples) plugs into the Test tab.
"""

import json

import streamlit as st

from bellweather.web import data
from bellweather.web import forms as form

NEW = "➕ New extractor…"
EXAMPLE_SCHEMA = (
    '{\n  "type": "object",\n  "properties": {\n'
    '    "title": {"type": "string"},\n    "price": {"type": "number"}\n  }\n}'
)
EXAMPLE_BINDING = (
    '{\n  "symbol_key": "scrape:demo:{title}",\n  "symbol_kind": "scraped-metric",\n'
    '  "value": "$.price",\n  "ts": "fetched_at",\n  "unit": "usd",\n  "tags": []\n}'
)

st.title("Extraction specs")
st.caption(
    "How to parse captures: output schema + binding + model. Fetching lives on the **Scrape** page."
)

specs = data.get_extraction_specs()
names = list(specs["name"]) if not specs.empty else []
choice = st.selectbox("Extractor", [NEW, *names])
is_new = choice == NEW
spec = None if is_new else data.get_extraction_spec(choice)
# The selected extractor's name, used by the Test/Delete controls below.
selected_name = "" if is_new else spec["name"]

sources_df = data.get_scrape_sources()
all_sources = list(sources_df["name"]) if not sources_df.empty else []

edit_tab, test_tab = st.tabs(["Edit", "Test"])

with edit_tab:
    if is_new:
        defaults = {
            "description": "",
            "schema": EXAMPLE_SCHEMA,
            "binding": EXAMPLE_BINDING,
            "model": "",
            "sources": [],
        }
    else:
        defaults = {
            "description": spec.get("description") or "",
            "schema": json.dumps(spec["output_schema"], indent=2),
            "binding": json.dumps(spec["binding"], indent=2),
            "model": spec.get("llm_model") or "",
            # Guard against links to sources that no longer exist so the
            # multiselect default never references a missing option.
            "sources": [s for s in (spec.get("sources") or []) if s in all_sources],
        }

    with st.form("extractor_form"):
        if is_new:
            name = st.text_input("Extractor name", value="my-extractor")
        else:
            st.text_input("Extractor name", value=selected_name, disabled=True)
            name = selected_name
        description = st.text_input("Description", value=defaults["description"])
        output_schema_raw = st.text_area("Output schema (JSON Schema)", value=defaults["schema"])
        binding_raw = st.text_area("Binding (JSON)", value=defaults["binding"])
        llm_model = st.text_input("LLM model (blank = default)", value=defaults["model"])
        linked_sources = st.multiselect(
            "Applies to sources",
            all_sources,
            default=defaults["sources"],
            help="The many-to-many link: this extractor parses every capture those sources land.",
        )
        submitted = st.form_submit_button("Create extractor" if is_new else "Save changes")

    if submitted:
        payload, errors = form.build_extraction_payload(
            name=name,
            description=description,
            output_schema_raw=output_schema_raw,
            binding_raw=binding_raw,
            llm_model=llm_model,
            require_name=is_new,
        )
        if errors:
            for e in errors:
                st.error(e)
        elif is_new:
            eid = data.create_extraction_spec(
                payload["name"],
                payload["output_schema"],
                payload["binding"],
                description=payload["description"],
                llm_model=payload["llm_model"],
                sources=linked_sources,
            )
            st.success(f"Created extraction spec #{eid}.")
            st.rerun()
        else:
            data.update_extraction_spec(
                selected_name,
                description=payload["description"],
                output_schema=payload["output_schema"],
                binding=payload["binding"],
                llm_model=payload["llm_model"],
                sources=linked_sources,
            )
            st.success("Saved changes.")
            st.rerun()

    if not is_new and st.button("Delete extractor"):
        data.delete_extraction_spec(selected_name)
        st.rerun()

with test_tab:
    if is_new:
        st.info("Create the extractor first, then test it against a capture here.")
    else:
        linked = [s for s in (spec.get("sources") or []) if s in all_sources]
        if not linked:
            st.info("No linked sources — attach one on the Edit tab first.")
        else:
            src_name = st.selectbox("Source", linked)
            src = data.get_scrape_source(src_name)
            sites = (src or {}).get("sites") or []
            if not sites:
                st.info("That source has no sites.")
            else:
                url = st.selectbox("Site / capture", sites)
                cap = data.get_capture(src_name, url)
                if cap is not None:
                    st.caption(
                        f"captured {cap['captured_at']} · {cap['content_type']} · "
                        f"{cap['size_bytes']} bytes"
                    )
                    lang = "html" if "html" in cap["content_type"] else "markdown"
                    st.code(cap["content"], language=lang)
                if st.button("Run extraction"):
                    try:
                        with st.spinner("Extracting from the capture (no fetch)…"):
                            out = data.preview_extraction(selected_name, src_name, url)
                    except Exception as exc:  # noqa: BLE001 — surface any backend error to the operator
                        st.error(f"Extraction failed: {exc}")
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
