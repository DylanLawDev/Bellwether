# Bellwether — GCP infrastructure (Terraform)

One `terraform apply` stands up the whole GCP baseline:

- a **GCS bronze bucket** (versioned, uniform access) for immutable raw bytes,
- a **Cloud SQL Postgres** instance (`db-f1-micro`) — the transactional spine,
- an **Artifact Registry** Docker repo,
- the **ingestion API** on a Cloud Run service (`bellweather api`),
- the **worker** as a Cloud Run Job (`bellweather worker --once`),
- a **Cloud Scheduler** trigger that runs the worker job every minute to drain the queue,
- a runtime **service account** + IAM, and a **Secret Manager** secret holding `DATABASE_URL`.

## Prerequisites

- A GCP project with billing enabled.
- `gcloud` authenticated locally: `gcloud auth application-default login`.
- Terraform `>= 1.6`.

## Apply flow

```bash
cp terraform.tfvars.example terraform.tfvars   # fill in project_id + bucket_name (bucket must be globally unique)
terraform init
terraform plan
terraform apply        # deploys the public 'hello' image first (see chicken-and-egg below)
```

### Chicken-and-egg: the image

Cloud Run needs an image *before* CI exists to build one, so `var.image` defaults to
Google's public `us-docker.pkg.dev/cloudrun/container/hello`. The first `apply` deploys
that placeholder. To ship the real app:

- **Preferred:** let T14's GitHub Actions pipeline build, push, and redeploy on merge to
  `main` (once CI manages the image, you no longer pass `-var image=` by hand).
- **Manual one-off:** build + push to the Artifact Registry repo, then
  `terraform apply -var image=<region>-docker.pkg.dev/<project>/bellweather/app:<tag>`.

### Migrations

The schema migrations must run against Cloud SQL once (and again whenever new migrations
land). Options:

- **Cloud Run Job (simplest, what T14 automates):** deploy a one-off job that runs
  `bellweather migrate` against the same Cloud SQL instance + `DATABASE_URL` secret, then
  execute it. T14's pipeline does exactly this on every deploy.
- **Locally via the Cloud SQL Auth Proxy:** point a local `DATABASE_URL` at the proxied
  instance and run `bellweather migrate`.

## Outputs

| Output           | What it is                                                        |
| ---------------- | ----------------------------------------------------------------- |
| `api_url`        | public URL of the Cloud Run API service (`/healthz` to smoke-test) |
| `bronze_bucket`  | GCS bronze bucket name                                             |
| `sql_connection` | Cloud SQL connection name (`project:region:instance`)             |
| `artifact_repo`  | Artifact Registry repo name                                       |
| `db_password`    | generated DB password (sensitive)                                 |

## Cost

Target baseline ≈ **$12–18/mo**. **Cloud SQL `db-f1-micro` dominates** — Cloud Run scales
to zero, GCS and Scheduler are effectively free at enthusiast volume. Keep changes within
this envelope (the project's stated `<$40/mo` target).

## Swapping Cloud SQL for Neon (free tier)

To drop the paid Cloud SQL instance and use an external Postgres (e.g. Neon free tier):

1. Remove the `google_sql_*` resources and the `random_password.db` resource.
2. Replace the `local.db_url` computation with your external connection string — feed it
   into the `bellweather-database-url` secret directly (e.g. via a `var.database_url`).
3. Drop the `cloudsql` volume + `volume_mounts` and the `roles/cloudsql.client` IAM
   binding from the API service and worker job.

Everything downstream (the secret reference, the runtime SA, Cloud Run wiring) stays the
same — only where `DATABASE_URL` comes from changes.
