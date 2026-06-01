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

- **Preferred:** let the GitHub Actions pipeline (`.github/workflows/deploy.yml`, T14)
  build, push, and redeploy on merge to `main`. **Once CI manages the image you no longer
  pass `-var image=` by hand** — the pipeline updates the service + job to each new
  SHA-tagged image.
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

## CI/CD (GitHub Actions — `.github/workflows/deploy.yml`)

On every push to `main`, the pipeline builds the image, pushes it to Artifact Registry,
runs migrations via a one-off Cloud Run Job, then updates the API service and worker job.

### Required GitHub secrets

| Secret              | Value                                                              |
| ------------------- | ----------------------------------------------------------------- |
| `GCP_PROJECT`       | your GCP project ID                                               |
| `GCP_BUCKET`        | bronze bucket name (the `bronze_bucket` output)                   |
| `GCP_SQL_CONN`      | Cloud SQL connection name (the `sql_connection` output)           |
| `GCP_RUNTIME_SA`    | runtime SA email (`bellweather-runtime@<project>.iam.gserviceaccount.com`) |
| `GCP_DEPLOYER_SA`   | deployer SA email used by the pipeline                            |
| `GCP_WIF_PROVIDER`  | Workload Identity Federation provider resource name              |

### Recommended: Workload Identity Federation (keyless)

No long-lived JSON key — GitHub's OIDC token is exchanged for short-lived GCP credentials.

```bash
PROJECT=your-gcp-project
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
REPO=DylanLawDev/Bellwether   # owner/repo

# 1. A deployer service account
gcloud iam service-accounts create bellweather-deployer \
  --project "$PROJECT" --display-name "Bellweather CI deployer"
DEPLOYER="bellweather-deployer@${PROJECT}.iam.gserviceaccount.com"

# 2. Roles it needs to build/push/deploy + run the migrate job
for ROLE in roles/run.admin roles/artifactregistry.writer \
            roles/iam.serviceAccountUser roles/cloudsql.client; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:${DEPLOYER}" --role "$ROLE"
done

# 3. A Workload Identity pool + GitHub OIDC provider
gcloud iam workload-identity-pools create github \
  --project "$PROJECT" --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers create-oidc github \
  --project "$PROJECT" --location global --workload-identity-pool github \
  --display-name "GitHub OIDC" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition "assertion.repository=='${REPO}'" \
  --issuer-uri "https://token.actions.githubusercontent.com"

# 4. Let the GitHub repo impersonate the deployer SA
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" \
  --project "$PROJECT" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"

# 5. The provider resource name → GCP_WIF_PROVIDER secret
echo "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/github"
```

Set `GCP_DEPLOYER_SA` to `$DEPLOYER` and `GCP_WIF_PROVIDER` to the printed provider name.

### Simpler alternative: a deployer SA JSON key (less secure)

Create a key for the deployer SA and store the JSON in a `GCP_SA_KEY` secret, then swap
the auth step to use it:

```yaml
- uses: google-github-actions/auth@v2
  with:
    credentials_json: ${{ secrets.GCP_SA_KEY }}
```

This avoids the WIF setup but leaves a long-lived credential in GitHub — prefer WIF.
