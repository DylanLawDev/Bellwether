"""Scrape specs — LLM scrape-engine control plane.

List scrape specs, author one (sites + output schema + binding), dry-run preview
the extraction (commits nothing), and delete. Reads/writes only through
bellweather.web.data (mock or live). Schedule a spec from the Schedules page with
template "scrape" and params {"spec": <name>}.
"""

import json

import streamlit as st

from bellweather.web import data

st.title("Scrape specs")
st.caption(
    "Declare {sites, output schema, binding} once; preview the LLM extraction, "
    "then schedule with the 'scrape' template."
)


def _parse_json(label: str, raw: str) -> tuple[object | None, str | None]:
    """Parse a JSON text area; return (value, error_message)."""
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"{label} is not valid JSON: {exc}"


# --- existing specs ---------------------------------------------------------
st.subheader("Specs")
specs = data.get_scrape_specs()
if specs.empty:
    st.info("No scrape specs yet. Author one below.")
else:
    for row in specs.to_dict("records"):
        name = row["name"]
        cols = st.columns([3, 2, 2, 2, 2])
        cols[0].markdown(f"**{name}**  \n`{row['fetch_adapter']}`")
        cols[1].markdown(row.get("description") or "_no description_")
        cols[2].markdown(f"model: `{row['llm_model'] or 'default'}`")
        enabled = cols[3].toggle("Enabled", value=bool(row["enabled"]), key=f"en_{name}")
        if enabled != bool(row["enabled"]):
            data.update_scrape_spec(name, enabled=enabled)
            st.rerun()
        if cols[4].button("Delete", key=f"del_{name}"):
            data.delete_scrape_spec(name)
            st.rerun()

        if st.button("Preview (dry-run)", key=f"prev_{name}"):
            spec = data.get_scrape_spec(name)
            first_url = spec["sites"][0] if spec.get("sites") else None
            out = data.preview_scrape_spec(name, url=first_url)
            st.success(
                f"Would emit {len(out['sample'])} sample point(s) across "
                f"{len(out['symbols'])} symbol(s) and {len(out['tags'])} tag(s)."
            )
            st.markdown("**Extracted JSON**")
            st.json(out["extracted"])
            st.markdown("**Sample observations**")
            st.json(out["sample"])

# --- author a spec ----------------------------------------------------------
st.subheader("Author a spec")
with st.form("add_spec"):
    name = st.text_input("Spec name", value="my-spec")
    description = st.text_input("Description", value="")
    sites_raw = st.text_area("Sites (one URL per line)", value="https://example.com/")
    output_schema_raw = st.text_area(
        "Output schema (JSON Schema)",
        value='{\n  "type": "object",\n  "properties": {"price": {"type": "number"}}\n}',
    )
    binding_raw = st.text_area(
        "Binding (JSON)",
        value=(
            '{\n  "symbol_key": "scrape:demo:{title}",\n  "symbol_kind": "scraped-metric",\n'
            '  "value": "$.price",\n  "ts": "fetched_at",\n  "unit": "usd",\n  "tags": []\n}'
        ),
    )
    fetch_adapter = st.text_input("Fetch adapter", value="httpx")
    llm_model = st.text_input("LLM model (blank = default)", value="")
    added = st.form_submit_button("Create spec")

if added:
    sites = [line.strip() for line in sites_raw.splitlines() if line.strip()]
    output_schema, err_schema = _parse_json("Output schema", output_schema_raw)
    binding, err_binding = _parse_json("Binding", binding_raw)
    errors = [e for e in (err_schema, err_binding) if e]
    if not name.strip():
        errors.append("Spec name is required.")
    if not sites:
        errors.append("At least one site URL is required.")
    if errors:
        for e in errors:
            st.error(e)
    else:
        sid = data.create_scrape_spec(
            name.strip(),
            sites,
            output_schema,
            binding,
            description=description or None,
            fetch_adapter=fetch_adapter or "httpx",
            llm_model=llm_model or None,
        )
        st.success(f"Created scrape spec #{sid}.")
        st.rerun()
