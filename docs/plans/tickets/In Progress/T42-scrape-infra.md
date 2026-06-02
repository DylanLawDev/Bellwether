# T42 — Infra: `ANTHROPIC_API_KEY` secret (worker + api)

**Spec:** `docs/specs/2026-06-01-llm-scrape-engine-design.md` D-b + §Deployment (and K1/K9 — the LLM key lives in the trusted spine). **Depends on:** T14 (Dockerfile + deploy + the worker Job/API service wiring), T36 (`config.anthropic_api_key`), T38 (`LlmScrapeExtractor` runs in the worker), T39 (in-process preview runs in the API service). **Branch:** `ticket/T42-scrape-infra`. **PR, do not merge without approval.**

> **Review amendment (PR #48, comment 3344829117).** The original plan below mounts `ANTHROPIC_API_KEY` on **both** the worker Job and the api service. During review the maintainer decided to keep the key **OFF the public, unauthenticated API service** to close a credit-drain vector on the in-process preview route (`POST /api/scrape-specs/{name}/preview`, T39/K10). The key is now mounted on the **worker Job ONLY**. The `secretAccessor` grant stays on the runtime SA (the worker runs as it). `config.anthropic_api_key` is `Optional`, so the API still boots and the preview route fails gracefully (`RuntimeError`) rather than the deploy failing. **The preview route is therefore disabled in prod** until a follow-up ticket adds an auth/rate-limit boundary in front of it. Wherever the steps below say "worker + api" / "both", read **worker only** for the env-var mount.

## Goal
Wire `ANTHROPIC_API_KEY` into the deployed infra so the LLM scrape engine can run in production. The key is the **first paid runtime dependency** (D-b) and, per K1/K9, must live **only in the trusted spine** — the **worker** Cloud Run Job (which runs `LlmScrapeExtractor`, T38) and the **api** Cloud Run service (which runs the in-process dry-run **preview**, T39). It is wired **exactly the way `DATABASE_URL` already is** — a Secret Manager secret + version, a `secretAccessor` grant to the runtime SA, and a `value_source.secret_key_ref` env var on each consumer — so there is one source-of-truth secret, never a plaintext env var. The orchestrator Job and the collector it spawns get **no** LLM key (the collector reads its spec via the API and only fetches + `POST /ingest`s the raw page — K4/D-e). Infra only — no application code changes; `producers/scrape/` already ships via the orchestrator Job's existing `COPY producers ./producers` image bake (T27), so no `Dockerfile` change is needed.

## Files
- Modify: `infra/variables.tf` — add `variable "anthropic_api_key"` (sensitive, optional default `""`), mirroring `variable "database_url"`.
- Modify: `infra/main.tf` — add `google_secret_manager_secret "anthropic_key"` + `_version` + `_iam_member` (mirrors the `db_url` trio), and an `ANTHROPIC_API_KEY` `value_source` env var on **both** the `api` service and the `worker` Job (mirrors the existing `DATABASE_URL` block on each). The orchestrator Job is left unchanged.
- Modify: `infra/terraform.tfvars.example` — note `anthropic_api_key` is optional (sourced from a secret), as a commented line.
- Modify: `infra/README.md` — document the new secret, which two surfaces consume it, and why the orchestrator/collector do not (K1/K4).
- Modify: `.env.example` — add `ANTHROPIC_API_KEY=` for local dev (read only by `config.py`, T36).
- Test: none — this is a Terraform ticket. Validation is `terraform fmt`/`validate`/`plan` plus a green `make check` (proving no Python was touched). Mirrors T27's non-pytest acceptance.

## Interface
The config field this secret satisfies (build plan "Locked interfaces" → `config.py`, added in T36 — only `config.py` reads env):
```python
anthropic_api_key: str | None = None
scrape_llm_model: str = "claude-haiku-4-5-20251001"   # cheap default; per-spec override wins
```
`config.py` (T36) reads `ANTHROPIC_API_KEY` from the environment; `LlmExtractor` (T36) lazily builds `anthropic.Anthropic(api_key=... or get_settings().anthropic_api_key)` and raises `RuntimeError` if no key. This ticket's only job is to **put that env var on the worker + api** in prod from a Secret Manager secret. The existing locked Terraform shape this mirrors (`infra/main.tf`, the `DATABASE_URL` secret + its three resources + the `value_source` consumer blocks):
```hcl
resource "google_secret_manager_secret" "db_url" {
  secret_id = "bellweather-database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}
resource "google_secret_manager_secret_version" "db_url" {
  secret      = google_secret_manager_secret.db_url.id
  secret_data = var.database_url
}
resource "google_secret_manager_secret_iam_member" "db_url_access" {
  secret_id = google_secret_manager_secret.db_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}
# ... consumed on the api service AND the worker Job as:
env {
  name = "DATABASE_URL"
  value_source {
    secret_key_ref {
      secret  = google_secret_manager_secret.db_url.secret_id
      version = "latest"
    }
  }
}
```

## Steps

- [ ] **Step 1: Add the `anthropic_api_key` variable.** In `infra/variables.tf`, after the `image` / `obs_bucket` variables (i.e. append at the end of the file), add a sensitive variable mirroring `database_url` but **optional** (the secret can be created empty and the key dropped in later, so the baseline applies without a real key):
```hcl
variable "anthropic_api_key" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Anthropic API key for the LLM scrape extractor (worker) + dry-run preview (api) — stored in Secret Manager. Optional: leave empty to apply the baseline, then drop the real key into the bellweather-anthropic-api-key secret later (the LLM path raises until it is set)."
}
```

- [ ] **Step 2: Add the secret + version + IAM grant to `infra/main.tf`.** Mirror the `db_url` trio exactly. Insert directly **after** the closing `}` of `google_secret_manager_secret_iam_member "db_url_access"` (i.e. just before the `locals { common_env ... }` block):
```hcl
# --- ANTHROPIC_API_KEY secret (LLM scrape engine, T42) ---
# Mirrors the DATABASE_URL secret trio. The key is the first paid RUNTIME
# dependency (design D-b) and lives ONLY in the trusted spine: the worker Job
# (runs LlmScrapeExtractor, T38) and the api service (in-process dry-run
# preview, T39). The orchestrator Job and the collector it spawns never get it
# (K1/K4 — the collector only fetches + POSTs the raw page; the LLM runs worker-side).
resource "google_secret_manager_secret" "anthropic_key" {
  secret_id = "bellweather-anthropic-api-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "anthropic_key" {
  secret      = google_secret_manager_secret.anthropic_key.id
  secret_data = var.anthropic_api_key
}

resource "google_secret_manager_secret_iam_member" "anthropic_key_access" {
  secret_id = google_secret_manager_secret.anthropic_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}
```

- [ ] **Step 3: Consume the secret on the api service.** In `google_cloud_run_v2_service "api"`, directly **after** the `DATABASE_URL` `env {}` block (the last env block in that container), add the `ANTHROPIC_API_KEY` env, mirroring the `DATABASE_URL` `value_source` shape:
```hcl
      env {
        name = "ANTHROPIC_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.anthropic_key.secret_id
            version = "latest"
          }
        }
      }
```
  Then add the secret's version + access grant to the api service's `depends_on` list so the secret exists before the revision boots:
```hcl
  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_iam_member.db_url_access,
    google_secret_manager_secret_version.anthropic_key,
    google_secret_manager_secret_iam_member.anthropic_key_access,
  ]
```

- [ ] **Step 4: Consume the secret on the worker Job.** In `google_cloud_run_v2_job "worker"`, directly **after** the `DATABASE_URL` `env {}` block, add the same `ANTHROPIC_API_KEY` env block (note the worker's container is nested one level deeper — `template { template { containers {` — so it is indented two more spaces than the api block above):
```hcl
        env {
          name = "ANTHROPIC_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.anthropic_key.secret_id
              version = "latest"
            }
          }
        }
```
  And extend the worker Job's `depends_on` the same way:
```hcl
  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_iam_member.db_url_access,
    google_secret_manager_secret_version.anthropic_key,
    google_secret_manager_secret_iam_member.anthropic_key_access,
  ]
```
  **Do not** add the env to the `orchestrator` Job — the collector it spawns must not hold the LLM key (K1/K4/D-e).

- [ ] **Step 5: Add `ANTHROPIC_API_KEY` to `.env.example` for local dev.** Append a line (only `config.py` reads it; absent → the `requires_llm`-marked tests skip and the LLM path raises a clear `RuntimeError`):
```bash
ANTHROPIC_API_KEY=                              # LLM scrape engine; unset → requires_llm tests skip, LLM path raises
```

- [ ] **Step 6: Note the optional key in `infra/terraform.tfvars.example`.** Append a commented line so operators know where the real key goes (kept commented because the real key never belongs in a committed example, but documented here):
```hcl
# anthropic_api_key = "sk-ant-..."   # optional; stored in the bellweather-anthropic-api-key secret. Leave unset to apply without it.
```

- [ ] **Step 7: Format + validate the Terraform.** From the repo root:
```bash
terraform -chdir=infra fmt
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
```
  Expect `fmt` to leave the files unchanged (re-run + re-read if it rewrites anything) and `validate` to print `Success! The configuration is valid.`

- [ ] **Step 8: Plan and confirm additions + at most two env-only diffs.** With placeholder vars (no apply):
```bash
terraform -chdir=infra plan \
  -var project_id=demo -var bucket_name=demo-bronze -var 'database_url=postgres://x' \
  -var image=us-docker.pkg.dev/cloudrun/container/hello
```
  Expect the diff for the LLM wiring to be exactly: the three new secret resources — `google_secret_manager_secret.anthropic_key`, `google_secret_manager_secret_version.anthropic_key`, `google_secret_manager_secret_iam_member.anthropic_key_access` — plus the added `ANTHROPIC_API_KEY` env on `google_cloud_run_v2_service.api` and `google_cloud_run_v2_job.worker`, and **no** change to `google_cloud_run_v2_job.orchestrator`, the bronze bucket, the artifact repo, the `db_url` secret, the runtime/scheduler SAs, or the schedulers. How those two consumers render depends on the state you plan against: **against an existing deployment** the api service and worker Job show as an **in-place update** (`~`) adding only the env var; **against a clean/empty state** (as with these placeholder vars on first run) they are full `+ create`s along with the rest of the baseline — in that case verify the env var is present and that the orchestrator block does NOT gain an `ANTHROPIC_API_KEY`. (`anthropic_api_key` defaults to `""`, so the plan does not require passing it. The plan completes without GCP credentials; `validate` from Step 7 is the hard gate.)

- [ ] **Step 9: Document in `infra/README.md`.** Add the secret to the top baseline bullet list — extend the `Secret Manager` line so it reads:
```markdown
- a runtime **service account** + IAM, and **Secret Manager** secrets holding `DATABASE_URL` and `ANTHROPIC_API_KEY`.
```
  Then add a new section after "The orchestrator Job (T27)":
```markdown
## The LLM scrape engine secret (T42)

The schema-driven scrape engine (`docs/specs/2026-06-01-llm-scrape-engine-design.md`) calls
Anthropic's API — the **first paid runtime dependency** (D-b). The key is wired exactly like
`DATABASE_URL`: a `bellweather-anthropic-api-key` Secret Manager secret (`+ version`, fed from
`var.anthropic_api_key`), a `secretmanager.secretAccessor` grant to the runtime SA, and an
`ANTHROPIC_API_KEY` env var sourced from that secret via `value_source.secret_key_ref`.

It is mounted on **only two surfaces — the trusted spine** (K1/K9):

| surface | why it needs the key |
| ------- | -------------------- |
| `bellweather-worker` Job | runs `LlmScrapeExtractor` (`scrape-llm-v1`) — the real extraction |
| `bellweather-api` service | runs the in-process **dry-run preview** (`POST /api/scrape-specs/{name}/preview`) |

The **orchestrator Job and the scrape collector it spawns do NOT get the key** (K4/D-e): the
collector runs unprivileged, reads its spec via the API, and only fetches each site (httpx, no
secret) and `POST /ingest`s the raw page — the LLM step happens later, worker-side. No new Cloud
Run Job is added; the collector ships in the existing `producers/` image bake (T27), so no
`Dockerfile` change is needed.

`var.anthropic_api_key` is **optional** (defaults to `""`): the baseline applies without it, then
the real key is dropped into the secret later (add a new secret version, or pass
`-var anthropic_api_key=sk-ant-...`). Until set, `LlmExtractor` raises a clear `RuntimeError` and
no scrape extraction succeeds — fetch/ingest/GDELT paths are unaffected.

**Cost:** the secret itself is free; the per-call Anthropic spend is the D-b cost flag — held in
budget by the cheap default model (Haiku) and low cadence. Cloud SQL still dominates the
`<$40/mo` envelope.
```
  Manual-set note: in prod the real key is added by creating a new version of the
  `bellweather-anthropic-api-key` secret (`gcloud secrets versions add bellweather-anthropic-api-key --data-file=-`)
  or by `terraform apply -var anthropic_api_key=sk-ant-...`; the `version = "latest"` ref picks it up
  on the next revision.

- [ ] **Step 10: Confirm app code is untouched.** `make check` must be green — this ticket changes only `infra/` + `.env.example`, no Python:
```bash
make check
```

- [ ] **Step 11: Commit** (`feat(infra): ANTHROPIC_API_KEY Secret Manager secret on worker + api`).

## Acceptance criteria
- `terraform -chdir=infra fmt` leaves the files unchanged and `terraform -chdir=infra validate` prints `Success! The configuration is valid.`
- `terraform -chdir=infra plan` shows the three new secret resources (`google_secret_manager_secret.anthropic_key`, `..._version.anthropic_key`, `..._iam_member.anthropic_key_access`) and the added `ANTHROPIC_API_KEY` env on `google_cloud_run_v2_service.api` and `google_cloud_run_v2_job.worker` (rendered as an in-place env-only update against an existing deployment, or as part of the full baseline create against a clean state) — with **no** change to the orchestrator Job, the bucket, the artifact repo, the `db_url` secret, the SAs, or the schedulers.
- `ANTHROPIC_API_KEY` is mounted from the `bellweather-anthropic-api-key` secret (`value_source.secret_key_ref`, `version = "latest"`) on **both** the worker Job and the api service, and the runtime SA holds `secretmanager.secretAccessor` on that secret.
- The orchestrator Job carries **no** `ANTHROPIC_API_KEY` env var (the collector it spawns stays unprivileged — K4/D-e), and no `Dockerfile` / no new Cloud Run Job is added (the collector ships via the existing T27 `producers/` bake).
- `var.anthropic_api_key` is sensitive and optional (`default = ""`), so the baseline applies without a real key; the key is set later via a new secret version (documented in `infra/README.md`).
- `.env.example` includes `ANTHROPIC_API_KEY=` for local dev; only `config.py` reads it.
- `infra/README.md` documents the secret, the two consuming surfaces, why the orchestrator/collector are excluded, the optional-key/manual-set flow, and the D-b cost note.
- `make check` stays green (infra only — no Python changed).
