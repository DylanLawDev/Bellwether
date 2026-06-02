"""Schedules — producer orchestrator control plane.

List usages, add one from a template's params schema, force/run/preview, and
view recent runs. Reads/writes only through bellweather.web.data (mock or live).
"""

import streamlit as st

from bellweather.web import data

st.title("Schedules")
st.caption("Bind a template to parameters + an interval; force, run-now, preview, and review runs.")

templates = {t["name"]: t for t in data.get_templates()}

# --- run controls -----------------------------------------------------------
if st.button("Run orchestrator now", help="Trigger an immediate tick instead of waiting."):
    result = data.run_orchestrator_now()
    started = result.get("started_run_ids", [])
    st.success(f"Started run(s): {started}" if started else "No schedules were due.")

# --- existing usages --------------------------------------------------------
st.subheader("Usages")
schedules = data.get_schedules()
if schedules.empty:
    st.info("No schedules yet. Add one below.")
else:
    for row in schedules.to_dict("records"):
        sid = row["id"]
        cols = st.columns([3, 2, 2, 2, 2])
        cols[0].markdown(f"**{row['name']}**  \n`{row['template']}`")
        cols[1].metric("Interval (s)", row["interval_seconds"])
        enabled = cols[2].toggle("Enabled", value=bool(row["enabled"]), key=f"en_{sid}")
        if enabled != bool(row["enabled"]):
            data.update_schedule(sid, enabled=enabled)
            st.rerun()
        # Force Run is one-shot: the orchestrator consumes it (reads off after a run).
        forced = cols[3].toggle("Force Run", value=bool(row["force_run"]), key=f"fr_{sid}")
        if forced and not bool(row["force_run"]):
            data.force_schedule(sid)
            st.rerun()
        if cols[4].button("Delete", key=f"del_{sid}"):
            data.delete_schedule(sid)
            st.rerun()

# --- add usage (form generated from a template's params schema) -------------
st.subheader("Add usage")
if not templates:
    st.warning("No templates discovered.")
else:
    tpl_name = st.selectbox("Template", list(templates))
    tpl = templates[tpl_name]
    st.caption(tpl.get("description", ""))
    with st.form("add_usage"):
        name = st.text_input("Usage name", value=f"{tpl_name}-usage")
        interval = st.number_input(
            "Interval (seconds)",
            min_value=1,
            value=int(tpl.get("default_interval_seconds") or 3600),
            step=60,
        )
        params: dict = {}
        for p in tpl.get("params", []):
            label = f"{p['name']}{' *' if p.get('required') else ''}"
            if p.get("choices"):
                params[p["name"]] = st.selectbox(
                    label, p["choices"], help=p.get("help"), key=f"p_{p['name']}"
                )
            elif p.get("type") == "int":
                params[p["name"]] = st.number_input(
                    label,
                    value=int(p.get("default") or 0),
                    step=1,
                    help=p.get("help"),
                    key=f"p_{p['name']}",
                )
            else:
                params[p["name"]] = st.text_input(
                    label,
                    value=str(p.get("default") or ""),
                    help=p.get("help"),
                    key=f"p_{p['name']}",
                )
        c_prev, c_add = st.columns(2)
        previewed = c_prev.form_submit_button("Preview (dry-run)")
        added = c_add.form_submit_button("Add")
    if previewed:
        out = data.preview_template(tpl_name, {k: v for k, v in params.items() if v != ""})
        st.success(
            f"Would emit {len(out['sample'])} sample point(s) across {len(out['symbols'])} symbol(s)."
        )
        st.json(out)
    if added:
        sid = data.create_schedule(
            name, tpl_name, {k: v for k, v in params.items() if v != ""}, int(interval)
        )
        st.success(f"Created schedule #{sid}.")
        st.rerun()

# --- recent runs ------------------------------------------------------------
st.subheader("Recent runs")
st.dataframe(data.get_runs(), hide_index=True)
