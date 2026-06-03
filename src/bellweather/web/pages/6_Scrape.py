"""Scrape sources — fetch-side control plane.

Select a source (or "➕ New source…"), edit what to fetch (sites + adapter), and
inspect the raw captures scraping produces. Parsing lives on the Extract page —
extraction specs attach to sources there (many-to-many); this page only shows
the links read-only. Reads/writes only through bellweather.web.data (mock or
live). Scheduling sources via the "scrape" template arrives with the T43+
backend (T45 renames its param spec → source); today's template still resolves
legacy scrape_specs rows, so source names can't be scheduled yet.
"""

import streamlit as st

from bellweather.web import data
from bellweather.web import forms as form

NEW = "➕ New source…"

st.title("Scrape sources")
if data.BACKEND == "live":
    # The T43+ backend tickets land /api/scrape-sources (spec §7/§8); until then
    # live mode would 404 on first load — stop with a clear notice instead.
    st.warning(
        "Scrape sources aren't live yet — the backend (`/api/scrape-sources`, T43+) "
        "hasn't landed. Run the UI with `BELLWEATHER_UI_SOURCE=mock` to explore this page."
    )
    st.stop()
st.caption(
    "What to fetch: sites + adapter. Raw captures are the product — "
    "parsing lives on the **Extract** page."
)

sources = data.get_scrape_sources()
names = list(sources["name"]) if not sources.empty else []
choice = st.selectbox("Source", [NEW, *names])
is_new = choice == NEW
src = None if is_new else data.get_scrape_source(choice)
# The selected source's name, used by the Captures/Delete controls below.
selected_name = "" if is_new else src["name"]

st.caption(
    '→ Scheduling sources lands with the T43+ backend (the "scrape" template still '
    "takes legacy spec names, not sources)."
)

edit_tab, captures_tab = st.tabs(["Edit", "Captures"])

with edit_tab:
    adapters = data.get_fetch_adapter_choices()
    if is_new:
        defaults = {"description": "", "sites": "https://example.com/", "enabled": True}
        adapter_options, adapter_idx = adapters, 0
    else:
        defaults = {
            "description": src.get("description") or "",
            "sites": "\n".join(src.get("sites") or []),
            "enabled": bool(src["enabled"]),
        }
        # Keep the source's current adapter selectable even if the registry no
        # longer lists it, so the selectbox never errors on a stale value.
        adapter_options = sorted(set(adapters) | {src["fetch_adapter"]})
        adapter_idx = adapter_options.index(src["fetch_adapter"])

    with st.form("source_form"):
        if is_new:
            name = st.text_input("Source name", value="my-source")
        else:
            st.text_input("Source name", value=selected_name, disabled=True)
            name = selected_name
        description = st.text_input("Description", value=defaults["description"])
        sites_raw = st.text_area("Sites (one URL per line)", value=defaults["sites"])
        fetch_adapter = st.selectbox("Fetch adapter", adapter_options, index=adapter_idx)
        enabled = st.toggle("Enabled", value=defaults["enabled"])
        submitted = st.form_submit_button("Create source" if is_new else "Save changes")

    if not is_new:
        parsed_by = src.get("parsed_by") or []
        chips = (
            " · ".join(f"`{e}`" for e in parsed_by) if parsed_by else "_none — captures stay raw_"
        )
        st.caption(f"Parsed by: {chips}  (attach extractors on the **Extract** page)")

    if submitted:
        payload, errors = form.build_source_payload(
            name=name,
            description=description,
            sites_raw=sites_raw,
            fetch_adapter=fetch_adapter,
            require_name=is_new,
        )
        if errors:
            for e in errors:
                st.error(e)
        elif is_new:
            sid = data.create_scrape_source(
                payload["name"],
                payload["sites"],
                description=payload["description"],
                fetch_adapter=payload["fetch_adapter"],
            )
            # create defaults to enabled=True; honor an unchecked toggle via PATCH.
            if not enabled:
                data.update_scrape_source(payload["name"], enabled=False)
            st.success(f"Created scrape source #{sid}.")
            st.rerun()
        else:
            data.update_scrape_source(
                selected_name,
                description=payload["description"],
                sites=payload["sites"],
                fetch_adapter=payload["fetch_adapter"],
                enabled=enabled,
            )
            st.success("Saved changes.")
            st.rerun()

    if not is_new and st.button("Delete source"):
        data.delete_scrape_source(selected_name)
        st.rerun()

with captures_tab:
    if is_new:
        st.info("Create the source first; its raw captures appear here.")
    else:
        sites = src.get("sites") or []
        if not sites:
            st.info("This source has no sites.")
        else:
            url = st.selectbox("Site", sites)
            cap = data.get_capture(selected_name, url)
            if st.button("Fetch now (test)"):
                try:
                    with st.spinner("Fetching raw content…"):
                        cap = data.fetch_capture_now(selected_name, url)
                except Exception as exc:  # noqa: BLE001 — surface any backend error to the operator
                    st.error(f"Fetch failed: {exc}")
            if cap is None:
                st.info("No capture for this site yet — use 'Fetch now (test)'.")
            else:
                st.caption(
                    f"captured {cap['captured_at']} · {cap['content_type']} · "
                    f"{cap['size_bytes']} bytes"
                )
                lang = "html" if "html" in cap["content_type"] else "markdown"
                st.code(cap["content"], language=lang)
                st.caption("Scraping ends here — parse this capture on the **Extract** page.")
