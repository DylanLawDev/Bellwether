# --- APIs ---
locals {
  apis = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
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

# --- Cloud SQL Postgres (db-f1-micro) ---
resource "random_password" "db" {
  length  = 24
  special = false
}

resource "google_sql_database_instance" "pg" {
  name             = "bellweather-pg"
  database_version = "POSTGRES_16"
  region           = var.region
  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_size         = 10
    disk_autoresize   = true
    backup_configuration {
      enabled = true
    }
    ip_configuration {
      ipv4_enabled = true # public IP; Cloud Run connects via connector
    }
  }
  deletion_protection = false
  depends_on          = [google_project_service.apis]
}

resource "google_sql_database" "db" {
  name     = var.db_name
  instance = google_sql_database_instance.pg.name
}

resource "google_sql_user" "user" {
  name     = var.db_user
  instance = google_sql_database_instance.pg.name
  password = random_password.db.result
}

# --- DATABASE_URL via Cloud SQL unix socket (Cloud Run mounts /cloudsql) ---
locals {
  conn   = google_sql_database_instance.pg.connection_name
  db_url = "postgresql://${var.db_user}:${random_password.db.result}@/${var.db_name}?host=/cloudsql/${local.conn}"
}

resource "google_secret_manager_secret" "db_url" {
  secret_id = "bellweather-database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_url" {
  secret      = google_secret_manager_secret.db_url.id
  secret_data = local.db_url
}

# --- Runtime service account + IAM ---
resource "google_service_account" "runtime" {
  account_id   = "bellweather-runtime"
  display_name = "Bellweather Cloud Run runtime"
}

resource "google_project_iam_member" "sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.runtime.email}"
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

locals {
  common_env = {
    BELLWEATHER_BUCKET     = google_storage_bucket.bronze.name
    BELLWEATHER_OBS_BUCKET = var.obs_bucket
  }
}

# --- Ingestion API (Cloud Run service) ---
resource "google_cloud_run_v2_service" "api" {
  name     = "bellweather-api"
  location = var.region
  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [local.conn]
      }
    }
    containers {
      image = var.image
      # No command override: rely on the image's ENTRYPOINT/CMD (`bellweather api --port 8080`).
      # This lets the placeholder `hello` image keep its own entrypoint so the first
      # `terraform apply` produces a Ready revision before T14's real image exists.
      ports {
        container_port = 8080
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
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
    }
  }
  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_iam_member.db_url_access,
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
  name     = "bellweather-worker"
  location = var.region
  template {
    template {
      service_account = google_service_account.runtime.email
      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [local.conn]
        }
      }
      containers {
        image   = var.image
        command = ["bellweather", "worker", "--once"]
        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
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
      }
    }
  }
  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.db_url,
    google_secret_manager_secret_iam_member.db_url_access,
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
