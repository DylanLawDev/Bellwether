# T13 — Terraform: GCS + Cloud SQL + Cloud Run + Scheduler

**Spec:** §8 Tech & deployment, OQ1/OQ2. This is the "plug into GCP" piece.
**Depends on:** none (independent — run early/in parallel). **Branch:** `ticket/T13-terraform-gcp`.
**PR, do not merge without approval.**

## Goal
One `terraform apply` stands up the whole GCP baseline: a GCS bronze bucket, a Cloud SQL Postgres (db-f1-micro), an Artifact Registry repo, the ingestion API on Cloud Run, the worker as a Cloud Run Job, and a Cloud Scheduler trigger to drain the queue every minute. Target baseline cost ≈ **$12–18/mo** (Cloud SQL micro dominates; everything else ≈ free at enthusiast volume).

## Design choices baked in (swappable)
- **Postgres = Cloud SQL db-f1-micro** (OQ1, GCP-native). To switch to Neon free-tier later, drop the `google_sql_*` resources and feed an external `DATABASE_URL` secret.
- **Worker = Cloud Run Job + Cloud Scheduler @ `* * * * *`** (OQ2). The job runs `bellweather worker --once` and exits.
- **Chicken-and-egg:** Cloud Run needs an image before CI exists, so `var.image` defaults to Google's public `hello` image. The first `apply` deploys hello; T14's pipeline pushes the real image and redeploys.

## Files
- Create: `infra/versions.tf`, `infra/variables.tf`, `infra/main.tf`, `infra/outputs.tf`, `infra/README.md`
- Create: `infra/.gitignore` (ignore `.terraform/`, `*.tfstate*`, `*.tfvars` except example)
- Create: `infra/terraform.tfvars.example`

## This ticket is infra, not TDD
No pytest. The "test" is: `terraform init && terraform validate && terraform plan` succeed, and (when you choose to apply) `terraform apply` produces a reachable `/healthz`. Validate is the CI-checkable gate; apply is manual (needs your GCP project + billing).

## Steps

- [ ] **Step 1: `infra/versions.tf`**
```hcl
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}
provider "google" {
  project = var.project_id
  region  = var.region
}
```

- [ ] **Step 2: `infra/variables.tf`**
```hcl
variable "project_id" { type = string }
variable "region"     { type = string  default = "us-central1" }
variable "db_tier"    { type = string  default = "db-f1-micro" }
variable "db_name"    { type = string  default = "bellweather" }
variable "db_user"    { type = string  default = "bellweather" }
variable "bucket_name" { type = string description = "globally-unique GCS bronze bucket name" }
variable "image" {
  type    = string
  default = "us-docker.pkg.dev/cloudrun/container/hello"  # placeholder until T14 pushes real image
}
variable "obs_bucket" { type = string default = "hour" }
```

- [ ] **Step 3: `infra/main.tf`**
```hcl
# --- APIs ---
locals {
  apis = [
    "run.googleapis.com", "sqladmin.googleapis.com", "storage.googleapis.com",
    "artifactregistry.googleapis.com", "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com", "iam.googleapis.com",
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
  versioning { enabled = true }
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
resource "random_password" "db" { length = 24 special = false }

resource "google_sql_database_instance" "pg" {
  name             = "bellweather-pg"
  database_version = "POSTGRES_16"
  region           = var.region
  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_size         = 10
    disk_autoresize   = true
    backup_configuration { enabled = true }
    ip_configuration { ipv4_enabled = true }   # public IP; Cloud Run connects via connector
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
  conn  = google_sql_database_instance.pg.connection_name
  db_url = "postgresql://${var.db_user}:${random_password.db.result}@/${var.db_name}?host=/cloudsql/${local.conn}"
}
resource "google_secret_manager_secret" "db_url" {
  secret_id = "bellweather-database-url"
  replication { auto {} }
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
    scaling { min_instance_count = 0  max_instance_count = 2 }
    volumes {
      name = "cloudsql"
      cloud_sql_instance { instances = [local.conn] }
    }
    containers {
      image   = var.image
      command = ["bellweather", "api", "--port", "8080"]
      ports { container_port = 8080 }
      volume_mounts { name = "cloudsql"  mount_path = "/cloudsql" }
      dynamic "env" {
        for_each = local.common_env
        content { name = env.key  value = env.value }
      }
      env {
        name = "DATABASE_URL"
        value_source { secret_key_ref { secret = google_secret_manager_secret.db_url.secret_id  version = "latest" } }
      }
    }
  }
  depends_on = [google_project_service.apis, google_secret_manager_secret_version.db_url]
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
        cloud_sql_instance { instances = [local.conn] }
      }
      containers {
        image   = var.image
        command = ["bellweather", "worker", "--once"]
        volume_mounts { name = "cloudsql"  mount_path = "/cloudsql" }
        dynamic "env" {
          for_each = local.common_env
          content { name = env.key  value = env.value }
        }
        env {
          name = "DATABASE_URL"
          value_source { secret_key_ref { secret = google_secret_manager_secret.db_url.secret_id  version = "latest" } }
        }
      }
    }
  }
  depends_on = [google_project_service.apis]
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
    oauth_token { service_account_email = google_service_account.scheduler.email }
  }
  depends_on = [google_project_service.apis]
}
```

- [ ] **Step 4: `infra/outputs.tf`**
```hcl
output "api_url"          { value = google_cloud_run_v2_service.api.uri }
output "bronze_bucket"    { value = google_storage_bucket.bronze.name }
output "sql_connection"   { value = google_sql_database_instance.pg.connection_name }
output "artifact_repo"    { value = google_artifact_registry_repository.repo.name }
output "db_password"      { value = random_password.db.result  sensitive = true }
```

- [ ] **Step 5: `infra/terraform.tfvars.example`**
```hcl
project_id  = "your-gcp-project"
region      = "us-central1"
bucket_name = "your-unique-bellweather-bronze"
```

- [ ] **Step 6: `infra/README.md`** — document the flow:
  ```
  cp terraform.tfvars.example terraform.tfvars   # fill in project_id + bucket_name
  terraform init
  terraform plan
  terraform apply        # deploys hello image first
  # then run T14's pipeline (or: build+push image, `terraform apply -var image=<digest>`)
  bellweather migrate    # run once against the DB (via Cloud SQL proxy or a one-off job)
  ```
  Include the **cost note** (~$12–18/mo, Cloud SQL micro dominates) and the **Neon swap note** (replace `google_sql_*` + secret with an external `DATABASE_URL`).
  Also note: **migrations** need to run against Cloud SQL once — simplest is to add a second Cloud Run Job `bellweather migrate` or run via the Cloud SQL Auth Proxy locally. (T14 adds a migrate step to the pipeline.)

- [ ] **Step 7: Validate & commit**
Run: `cd infra && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.`
Commit (`feat: add Terraform GCP baseline`).

## Acceptance criteria
- `terraform validate` passes (this is what CI can check).
- `terraform plan` against a real project shows the full resource set with no errors.
- Resources: GCS bucket, Cloud SQL micro + db + user, Artifact Registry, Secret (DATABASE_URL), runtime SA + IAM, Cloud Run service (API) + job (worker), Scheduler @ 1-min.
- README documents apply flow, migration step, cost, and the Neon swap path.
> If exact Cloud Run v2 ↔ Cloud SQL volume wiring needs tweaks on first real `apply`, iterate — the structure above is the intended shape.
