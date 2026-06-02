# T17 — Package the UI + API as a single Cloud Run app

**Spec:** `docs/specs/2026-05-31-ui-prototype-design.md`; README §4; cost envelope in `CLAUDE.md`.
**Depends on:** T14 (Dockerfile + deploy), T16 (live web backend), T13 (Terraform). **Branch:** `ticket/T17-single-app-deploy`. **PR, do not merge without approval.**

## Goal
Ship the operator UI and the ingestion/read API as **one Cloud Run service** ("spin it up as
a single app on gcloud"), within the existing `<$40/mo`, scale-to-zero envelope. One image,
one service, one public URL: `/` serves the Streamlit UI, `/api/*` + `/healthz` serve FastAPI.

## Approach (recommended): one container, internal reverse proxy
Cloud Run gives a container one ingress port (`$PORT`, default 8080). Run three processes in
the image and route by path:

- **uvicorn** (FastAPI) on `127.0.0.1:8000`
- **streamlit** on `127.0.0.1:8501` (`--server.headless true --server.address 127.0.0.1`)
- **reverse proxy** (Caddy — single static binary, tiny config) listening on `$PORT`:
  - `/api/*`, `/healthz`, `/docs`, `/openapi.json` → `127.0.0.1:8000`
  - everything else (incl. Streamlit's `/_stcore/*` websocket) → `127.0.0.1:8501`

A small process manager (`honcho` via a `Procfile`, or a 10-line `entrypoint.sh` with
`trap`/`wait`) supervises the three and exits if any dies. The UI talks to the API in-process
over localhost (`BELLWEATHER_UI_SOURCE=live`, `BELLWEATHER_API_URL=http://127.0.0.1:8000`).

> **Alternative (documented, not chosen):** Cloud Run **multi-container** (sidecars) — one
> container for the API, one for the UI, sharing the service ingress. Fewer in-image moving
> parts but two images to build/push and a more complex Terraform service block. Single-image
> + proxy is simpler to build and cheaper to operate; revisit sidecars only if the processes
> need independent scaling.

## Files
- Modify: `Dockerfile` (from T14) — also install the `ui` dependency group (Streamlit becomes
  a **runtime** dep of this image), copy `Caddyfile` + `Procfile`/`entrypoint.sh`, install Caddy.
- Create: `Caddyfile`, `Procfile` (or `deploy/entrypoint.sh`).
- Modify: `.dockerignore` — ensure `src/bellweather/web/**` is **included** (not ignored).
- Modify: `infra/` (T13 Terraform) — the Cloud Run service env: `BELLWEATHER_UI_SOURCE=live`,
  `BELLWEATHER_API_URL=http://127.0.0.1:8000`; keep DB/bucket/Cloud SQL wiring; raise the
  service `--port` to the proxy port; bump min memory if needed for Streamlit (note cost).
- Modify: `.github/workflows/deploy.yml` (T14) — no new steps if the single image already
  builds; confirm the migrate job + service update still apply.
- Modify: `infra/README.md` — document the combined service, the proxy, and that the worker
  Job is unchanged (still its own Cloud Run Job).

## Steps
- [ ] **Step 1:** Write `Caddyfile` + `Procfile`/`entrypoint.sh`; have the image launch all three.
- [ ] **Step 2:** Extend the Dockerfile; `docker build -t bellweather:web .`.
- [ ] **Step 3:** Local smoke: `docker run -p 8080:8080 -e BELLWEATHER_UI_SOURCE=live ... bellweather:web`
  then `curl localhost:8080/healthz` → ok and `curl -I localhost:8080/` → Streamlit HTML.
- [ ] **Step 4:** Update Terraform; `terraform plan` shows the env + port changes only.
- [ ] **Step 5:** Confirm the worker Cloud Run **Job** is untouched (it runs `bellweather worker`).
- [ ] **Step 6: Commit** (`feat: package UI + API as a single Cloud Run service`).

## Acceptance criteria
- One image serves UI at `/` and API at `/api/*` + `/healthz` behind one port; websocket
  (`/_stcore/stream`) works so the UI is interactive.
- `terraform apply` deploys a single reachable service; worker Job unchanged.
- Stays within the cost envelope (scale-to-zero; note any memory bump). `make check` stays
  green (packaging/infra only — no app code changed here).
