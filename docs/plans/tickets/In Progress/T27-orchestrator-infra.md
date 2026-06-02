# T27 — Infra — orchestrator Job + Scheduler + image bake

**Spec:** `docs/specs/2026-06-01-producer-orchestrator-design.md` §9 (Deployment) + §7 (Repo loading) + D2/D3. **Depends on:** T14 (Dockerfile + deploy), T24 (orchestrator tick + `bellweather orchestrate`). **Branch:** `ticket/T27-orchestrator-infra`. **PR, do not merge without approval.**

## Goal
Deploy the orchestrator as its own tiny scheduled Cloud Run Job, mirroring the existing
worker Job + drain-scheduler pattern exactly. Add `google_cloud_run_v2_job "orchestrator"`
(runs `bellweather orchestrate --once`), `google_cloud_scheduler_job "orchestrate"` (pings it
every minute), and grant the API service's runtime SA `run.invoker` on the orchestrator Job so
the UI's *Run now* button (D3) can trigger an immediate tick. Bake the `producers/` templates
dir into the image and point `BELLWEATHER_TEMPLATES_DIR` at it so discovery works in-container.
Infra only — no application code changes.

## Files
- Modify: `infra/main.tf` — the orchestrator Cloud Run Job, its scheduler, and the
  `run.invoker` IAM grants.
- Modify: `Dockerfile` — `COPY producers ./producers`; `ENV BELLWEATHER_TEMPLATES_DIR=/app/producers`.
- Modify: `infra/README.md` — document the orchestrator Job, its scheduler, the image bake,
  and that scripts spawned by it get **no** DB/bucket creds (K4).
- Test: none — this is infrastructure. Validation is `terraform fmt`/`validate`/`plan` plus a
  green `make check` (proving no app code was touched).

## Interface
The locked Terraform shape (build plan "Locked interfaces" → `api.py`/§9; design §9, D2, D3):

- `google_cloud_run_v2_job "orchestrator"` — `command = ["bellweather", "orchestrate", "--once"]`,
  runtime SA, env: `BELLWEATHER_API_URL` = the **in-project** `bellweather-api` service URL (D2),
  `BELLWEATHER_TEMPLATES_DIR`, and `DATABASE_URL` from the secret (the orchestrator reads the
  schedule registry; the scripts it spawns do not — K4).
- `google_cloud_scheduler_job "orchestrate"` — `schedule = "* * * * *"`, OAuth-invokes the Job
  (reuses the existing `bellweather-scheduler` SA, exactly like the worker drain).
- The runtime SA (`google_service_account.runtime`, which the API service runs as) gets
  `roles/run.invoker` on the orchestrator Job — so the UI's *Run now* (POST `/api/orchestrator/run`)
  can fire a tick.

Config field this image bake satisfies (build plan → `config.py`):
```python
bellweather_templates_dir: str = "producers"   # dir scanned for */template.toml
```

## Steps

- [ ] **Step 1: Bake the templates dir into the image.** In `Dockerfile`, after the existing
  `COPY src ./src` line, add a copy of `producers/` and set the templates-dir env so
  `discover_templates()` resolves in-container (the `.dockerignore` already does **not** ignore
  `producers/`, so it is in the build context):
```dockerfile
COPY pyproject.toml ./
COPY src ./src
# Bake the collector-scripts repo (template manifests + scripts) into the image so the
# orchestrator can discover and spawn them (T27). BELLWEATHER_TEMPLATES_DIR points the
# template registry (templates.discover_templates) at this baked-in dir; default is the
# repo's own `producers/`, so the demo runs without an external repo (design §7).
COPY producers ./producers
ENV BELLWEATHER_TEMPLATES_DIR=/app/producers
# Install the pipeline AND the `ui` group: Streamlit is a RUNTIME dependency of
# this combined image (it serves the operator UI), not just a local-dev tool.
RUN uv pip install --system --no-cache --group ui .
```
  (Place the `COPY producers` + `ENV` lines between the existing `COPY src ./src` and the
  `RUN uv pip install ...` lines. Do not duplicate the `COPY src`/`RUN` lines — only insert.)

- [ ] **Step 2: Build the image locally to confirm the bake.** `producers/` must be present in
  the image with the env set:
```bash
docker build -t bellweather:orch /home/dylan/bellwether
docker run --rm --entrypoint sh bellweather:orch -c 'ls /app/producers && echo "DIR=$BELLWEATHER_TEMPLATES_DIR"'
```
  Expect `gdelt` (and `__init__.py`) listed and `DIR=/app/producers`.

- [ ] **Step 3: Add the orchestrator Cloud Run Job to `infra/main.tf`.** Mirror the existing
  `google_cloud_run_v2_job "worker"` block exactly — same `service_account`, `common_env`,
  and `DATABASE_URL` secret wiring — changing only the name, the `command`, and adding the two
  orchestrator env vars. Insert this directly **after** the closing `}` of the
  `google_cloud_run_v2_job "worker"` resource (and before the `--- Scheduler ---` section):
```hcl
# --- Orchestrator (Cloud Run Job: `bellweather orchestrate --once`) ---
# Mirrors the worker Job: same runtime SA, same DATABASE_URL secret. It reads the
# schedule registry and spawns each due template as a subprocess. The SCRIPTS it
# spawns get only BELLWEATHER_API_URL (no DB/bucket creds, K4); the orchestrator
# itself needs the DB to read producer_schedules / write producer_runs.
resource "google_cloud_run_v2_job" "orchestrator" {
  name                = "bellweather-orchestrator"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.runtime.email
      containers {
        image   = var.image
        command = ["bellweather", "orchestrate", "--once"]
        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.key
            value = env.value
          }
        }
        # D2: the orchestrator targets THIS project's own in-project API service —
        # never a public/third-party endpoint. It authenticates as the runtime SA.
        env {
          name  = "BELLWEATHER_API_URL"
          value = google_cloud_run_v2_service.api.uri
        }
        # The baked-in templates dir (see Dockerfile). Explicit here so the Job's
        # env matches the image default even if the bake location ever changes.
        env {
          name  = "BELLWEATHER_TEMPLATES_DIR"
          value = "/app/producers"
        }
        env {
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.db_url.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }
  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_iam_member.db_url_access,
  ]
}
```

- [ ] **Step 4: Add the orchestrator scheduler to `infra/main.tf`.** Mirror the existing
  `google_cloud_scheduler_job "drain"` (reuse `google_service_account.scheduler`), and add the
  scheduler-SA `run.invoker` grant on the orchestrator Job. Insert this directly **after** the
  existing `google_cloud_scheduler_job "drain"` resource (end of file):
```hcl
# --- Scheduler: run the orchestrator job every minute (mirrors the worker drain) ---
resource "google_cloud_run_v2_job_iam_member" "scheduler_orchestrate" {
  name     = google_cloud_run_v2_job.orchestrator.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# D3: the API service (which runs as the runtime SA) can invoke the orchestrator Job,
# so the UI's "Run now" button (POST /api/orchestrator/run) triggers an immediate tick.
resource "google_cloud_run_v2_job_iam_member" "runtime_orchestrate" {
  name     = google_cloud_run_v2_job.orchestrator.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_scheduler_job" "orchestrate" {
  name     = "bellweather-orchestrate"
  schedule = "* * * * *"
  region   = var.region
  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.orchestrator.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }
  depends_on = [google_project_service.apis]
}
```

- [ ] **Step 5: Format + validate the Terraform.** From the repo root:
```bash
terraform -chdir=infra fmt
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
```
  Expect `fmt` to leave the files unchanged (re-run if it rewrites anything, then re-read) and
  `validate` to print `Success! The configuration is valid.`

- [ ] **Step 6: Plan and confirm additions only.** With placeholder vars (no apply):
```bash
terraform -chdir=infra plan \
  -var project_id=demo -var bucket_name=demo-bronze -var 'database_url=postgres://x' \
  -var image=us-docker.pkg.dev/cloudrun/container/hello
```
  Expect the plan to show ONLY new resources — `google_cloud_run_v2_job.orchestrator`,
  `google_cloud_run_v2_job_iam_member.scheduler_orchestrate`,
  `google_cloud_run_v2_job_iam_member.runtime_orchestrate`,
  `google_cloud_scheduler_job.orchestrate` (plus the rest of the baseline on a clean state) —
  and **no destroys/modifications** to the existing API service, worker Job, or drain scheduler.
  (Without GCP credentials the plan may stop at provider auth; the resource diff up to that
  point must still be additions-only. `validate` from Step 5 is the hard gate.)

- [ ] **Step 7: Document in `infra/README.md`.** In the bullet list at the top, add the
  orchestrator Job + its scheduler alongside the worker entries:
```markdown
- the **worker** as a Cloud Run Job (`bellweather worker --once`),
- the **orchestrator** as a Cloud Run Job (`bellweather orchestrate --once`),
- a **Cloud Scheduler** trigger that runs the worker job every minute to drain the queue,
- a **Cloud Scheduler** trigger that runs the orchestrator job every minute to fire due schedules,
```
  Then add a new section after "The combined Cloud Run service (T17)":
```markdown
## The orchestrator Job (T27)

A third small scheduled process — `bellweather-orchestrator` — drives **producers** (the
front of the pipe), exactly as the worker drains the **queue** (the back). It is a Cloud Run
**Job** running `bellweather orchestrate --once`, pinged every minute by the
`bellweather-orchestrate` scheduler — the same OAuth-invoke pattern as the worker drain,
reusing the `bellweather-scheduler` SA.

Each tick reads due `producer_schedules`, claims them, and spawns each template as a
**subprocess** with only `BELLWEATHER_API_URL` (the in-project `bellweather-api` URL, D2) —
**never** the DB or bucket creds (K4). The orchestrator itself needs `DATABASE_URL` (to read
the schedule registry and record `producer_runs`); the scripts it spawns do not.

The template manifests + scripts are **baked into the image**: the `Dockerfile` does
`COPY producers ./producers` and sets `BELLWEATHER_TEMPLATES_DIR=/app/producers`, so
`templates.discover_templates()` resolves in-container without an external repo (design §7).

The runtime SA (which the API service runs as) is granted `run.invoker` on the orchestrator
Job, so the UI's **Run now** button (POST `/api/orchestrator/run`) can trigger an immediate
tick (D3) instead of waiting for the minute scheduler.

**Cost:** one more tiny scheduled Job — it scales to zero between ticks, so it adds no
always-on cost. Cloud SQL still dominates; the project stays in the `<$40/mo` envelope.
```

- [ ] **Step 8: Confirm app code is untouched.** `make check` must be green — this ticket
  changes only `infra/` + `Dockerfile`, no Python:
```bash
make check
```

- [ ] **Step 9: Commit** (`feat(infra): orchestrator Cloud Run Job + scheduler + templates bake`).

## Acceptance criteria
- `terraform -chdir=infra fmt` leaves the files unchanged and `terraform -chdir=infra validate`
  prints `Success! The configuration is valid.`
- `terraform -chdir=infra plan` shows ONLY additions — the orchestrator Job, its scheduler, and
  the two `run.invoker` grants (scheduler SA + runtime SA) — with no destroys or modifications
  to the existing API service, worker Job, or drain scheduler.
- The orchestrator Job runs `["bellweather", "orchestrate", "--once"]` as the runtime SA, with
  `BELLWEATHER_API_URL` = the in-project `bellweather-api` URL (D2), `BELLWEATHER_TEMPLATES_DIR`,
  and `DATABASE_URL` from the secret; the spawned scripts get no DB/bucket creds (K4).
- The `Dockerfile` bakes `producers/` into the image and sets `BELLWEATHER_TEMPLATES_DIR=/app/producers`;
  `ls /app/producers` in the built image lists the templates dir.
- The runtime SA holds `run.invoker` on the orchestrator Job (enables the UI *Run now*).
- `infra/README.md` documents the orchestrator Job, its scheduler, the image bake, and the K4
  creds-minimal isolation.
- Stays within the cost envelope (one more tiny scheduled Job, scale-to-zero — no always-on cost).
- `make check` stays green (infra only — no app code changed).
