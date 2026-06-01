# T16 — Wire the web UI to the read API (`web.data.live`) + `bellweather ui` CLI

**Spec:** `docs/specs/2026-05-31-ui-prototype-design.md` ("From prototype to live").
**Depends on:** T15 (read API). **Branch:** `ticket/T16-web-live-backend`. **PR, do not merge without approval.**

## Goal
Flip the web UI from mock data to live data **without touching any screen**, by implementing
the `live` backend stub against the T15 read endpoints, and add a first-class way to launch
the UI from the packaged app (`bellweather ui`). The seam already exists
(`bellweather.web.data` selects backend by `BELLWEATHER_UI_SOURCE`); this fills in the other half.

## Files
- Modify: `src/bellweather/web/data/live.py` — replace the `NotImplementedError` stubs with
  `httpx` calls to `${BELLWEATHER_API_URL}/api/...`, assembling the **same** pandas frames /
  dicts the mock backend returns (column contract in `web.data.source`).
- Modify: `src/bellweather/cli.py` — add a `ui` command that launches Streamlit on
  `bellweather/web/app.py`. Import Streamlit **lazily** (it's in the optional `ui` group);
  if missing, print an actionable error (`uv sync --group ui`).
- Modify: `src/bellweather/config.py` — already has `bellweather_api_url`; no new field needed.
  (Backend selection stays env-driven via `BELLWEATHER_UI_SOURCE`, default `mock`.)
- Test: `tests/test_web_live.py` — use **`pytest-httpserver`** (already a dev dep) to stand up
  a fake read API, point `BELLWEATHER_API_URL` at it, and assert `live.*` builds frames whose
  columns/keys equal the `source` contract constants and whose values match the served JSON.

## Steps
- [ ] **Step 1: Failing test** `tests/test_web_live.py` — register handlers on the httpserver
  for `/api/symbols`, `/api/observations`, `/api/records`, `/api/tags`, `/api/queue`,
  `/api/ingestion-rate`, `/api/config` returning canned JSON; assert each `live.*` function
  returns a DataFrame/dict matching the contract (use the `source.*_COLUMNS` constants).
- [ ] **Step 2: Run → FAIL** (stubs raise `NotImplementedError`).
- [ ] **Step 3: Implement `live.py`** — a small `_get(path, **params)` helper using
  `httpx.Client(base_url=get_settings().bellweather_api_url, timeout=...)`; each public
  function calls it and does `pd.DataFrame(rows, columns=source.X_COLUMNS)` (parse timestamp
  columns with `pd.to_datetime`). Keep signatures byte-identical to `mock.py`.
- [ ] **Step 4: Implement `bellweather ui`** in `cli.py`:
```python
@app.command()
def ui(port: int = 8501):
    """Launch the Streamlit web UI (needs the `ui` dependency group)."""
    from pathlib import Path
    try:
        from streamlit.web import cli as st_cli
    except ModuleNotFoundError:
        raise SystemExit("Streamlit not installed. Run: uv sync --group ui")
    app_path = str(Path(__file__).with_name("web") / "app.py")
    sys.argv = ["streamlit", "run", app_path, "--server.port", str(port)]
    st_cli.main()
```
- [ ] **Step 5: Run → PASS.** Manually verify both modes:
  `BELLWEATHER_UI_SOURCE=mock bellweather ui` (works offline) and, with the API running,
  `BELLWEATHER_UI_SOURCE=live BELLWEATHER_API_URL=http://localhost:8000 bellweather ui`.
- [ ] **Step 6: Commit** (`feat: live web UI backend + bellweather ui command`).

## Acceptance criteria
- `live.*` returns frames/dicts indistinguishable in shape from `mock.*` (same column constants).
- No screen/page file changes — only the backend + CLI.
- `bellweather ui` launches the UI; missing Streamlit produces a clear install hint.
- Default remains `mock`; `make check` green (the live test uses `pytest-httpserver`, no network).
