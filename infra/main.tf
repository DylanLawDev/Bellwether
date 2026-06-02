# --- APIs ---
locals {
  apis = [
    "run.googleapis.com",
    "storage.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# --- Bronze bucket (immutable-ish: versioning on, uniform access) ---
resource "google_storage_bucket" "bronze" {
  name                        = var.bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  versioning {
    enabled = true
  }
  depends_on = [google_project_service.apis]
}

# --- Artifact Registry (Docker) ---
resource "google_artifact_registry_repository" "repo" {
  repository_id = "bellweather"
  format        = "DOCKER"
  location      = var.region
  depends_on    = [google_project_service.apis]
}

# --- DATABASE_URL secret (populated from var.database_url — Neon connection string) ---
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

# --- Runtime service account + IAM ---
resource "google_service_account" "runtime" {
  account_id   = "bellweather-runtime"
  display_name = "Bellweather Cloud Run runtime"
}

resource "google_storage_bucket_iam_member" "bronze_rw" {
  bucket = google_storage_bucket.bronze.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "db_url_access" {
  secret_id = google_secret_manager_secret.db_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# --- Orchestrator service account (untrusted-template spawner) ---
# Separate identity from the runtime SA so the Anthropic key can be scoped to the
# trusted spine ONLY (api + worker). The orchestrator spawns external templates
# (the scrape collector et al.) as subprocesses; on Cloud Run those inherit this
# SA's ADC via the metadata server regardless of the minimal child env, so any
# secret this SA can read is effectively reachable by an external template. It is
# therefore granted ONLY the DATABASE_URL secret (it reads producer_schedules /
# writes producer_runs) and is deliberately NOT granted the Anthropic key
# (K1/K4 — the collector must never reach the LLM key; extraction is worker-side).
resource "google_service_account" "orchestrator" {
  account_id   = "bellweather-orchestrator"
  display_name = "Bellweather orchestrator (template spawner)"
}

resource "google_secret_manager_secret_iam_member" "orchestrator_db_url_access" {
  secret_id = google_secret_manager_secret.db_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator.email}"
}

# --- ANTHROPIC_API_KEY secret (LLM scrape engine, T42) ---
# Mirrors the DATABASE_URL secret trio. The key is the first paid RUNTIME
# dependency (design D-b) and lives ONLY in the trusted spine: the worker Job
# (runs LlmScrapeExtractor, T38) and the api service (in-process dry-run
# preview, T39), both of which run as the runtime SA. The orchestrator runs as a
# SEPARATE SA (google_service_account.orchestrator) that is NOT granted this
# secret, so neither the orchestrator nor the collector it spawns can read the
# key via ADC — env-var omission alone is insufficient because Cloud Run ADC is
# ambient via the metadata server (K1/K4 — the LLM runs worker-side).
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

# Granted ONLY to the runtime SA (api + worker — the trusted spine). The
# orchestrator SA is intentionally excluded so spawned templates cannot read it.
resource "google_secret_manager_secret_iam_member" "anthropic_key_access" {
  secret_id = google_secret_manager_secret.anthropic_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

locals {
  common_env = {
    BELLWEATHER_BUCKET     = google_storage_bucket.bronze.name
    BELLWEATHER_OBS_BUCKET = var.obs_bucket
  }
}

# --- Ingestion API + UI (Cloud Run service) ---
resource "google_cloud_run_v2_service" "api" {
  name                = "bellweather-api"
  location            = var.region
  deletion_protection = false
  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image
      # No command override: rely on the image's ENTRYPOINT (the T17 supervisor,
      # which runs uvicorn + Streamlit + Caddy). Caddy binds Cloud Run's $PORT
      # (= container_port below) and reverse-proxies /api/*, /healthz, /ingest,
      # and /docs to FastAPI; everything else to the Streamlit UI. Leaving the
      # command unset also lets the placeholder `hello` image keep its own
      # entrypoint, so the first `terraform apply` is Ready before CI's real image.
      ports {
        container_port = 8080
      }
      # Streamlit pushes the combined image's footprint past the 512Mi default;
      # 1Gi keeps headroom. Cost stays in-envelope — the service scales to zero,
      # so memory is billed only while actually serving a request.
      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }
      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
      # Serve the operator UI from this same service, talking to the in-process
      # API over localhost (not back out through the public URL).
      env {
        name  = "BELLWEATHER_UI_SOURCE"
        value = "live"
      }
      env {
        name  = "BELLWEATHER_API_URL"
        value = "http://127.0.0.1:8000"
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
      env {
        name = "ANTHROPIC_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.anthropic_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }
  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_iam_member.db_url_access,
    google_secret_manager_secret_version.anthropic_key,
    google_secret_manager_secret_iam_member.anthropic_key_access,
  ]
}

# Public access to the API (tighten later if desired)
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Worker (Cloud Run Job: `bellweather worker --once`) ---
resource "google_cloud_run_v2_job" "worker" {
  name                = "bellweather-worker"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.runtime.email
      containers {
        image   = var.image
        command = ["bellweather", "worker", "--once"]
        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.key
            value = env.value
          }
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
        env {
          name = "ANTHROPIC_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.anthropic_key.secret_id
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
    google_secret_manager_secret_version.anthropic_key,
    google_secret_manager_secret_iam_member.anthropic_key_access,
  ]
}

# --- Orchestrator (Cloud Run Job: `bellweather orchestrate --once`) ---
# Runs as its OWN service account (google_service_account.orchestrator), NOT the
# runtime SA, so the Anthropic key stays scoped to the trusted spine (api +
# worker). It reads the schedule registry and spawns each due template as a
# subprocess. The SCRIPTS it spawns get only BELLWEATHER_API_URL (no DB/bucket
# creds, K4) AND, because they share this Job's ambient ADC, only the secrets
# this SA can read — DATABASE_URL only, never the LLM key (K1/K4). The
# orchestrator itself needs the DB to read producer_schedules / write producer_runs.
resource "google_cloud_run_v2_job" "orchestrator" {
  name                = "bellweather-orchestrator"
  location            = var.region
  deletion_protection = false
  template {
    template {
      service_account = google_service_account.orchestrator.email
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
    google_secret_manager_secret_iam_member.orchestrator_db_url_access,
  ]
}

# --- Scheduler: run the worker job every minute ---
resource "google_service_account" "scheduler" {
  account_id   = "bellweather-scheduler"
  display_name = "Bellweather scheduler invoker"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_run" {
  name     = google_cloud_run_v2_job.worker.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "drain" {
  name     = "bellweather-worker-drain"
  schedule = "* * * * *"
  region   = var.region
  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${google_cloud_run_v2_job.worker.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }
  depends_on = [google_project_service.apis]
}

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
