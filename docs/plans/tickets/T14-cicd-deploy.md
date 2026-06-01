# T14 — Dockerfile + GitHub Actions build/deploy

**Spec:** §8 deployment. Closes the loop so merges to `main` ship to GCP — important for phone-driven work.
**Depends on:** T07, T11, T13. **Branch:** `ticket/T14-cicd-deploy`. **PR, do not merge without approval.**

## Goal
A single container image (entrypoint switches api/worker/migrate) and a deploy pipeline that, on merge to `main`, builds + pushes to Artifact Registry, runs migrations, and redeploys the Cloud Run service + job to the new image.

## Files
- Create: `Dockerfile`, `.dockerignore`
- Create: `.github/workflows/deploy.yml`
- Modify: `infra/README.md` (note that CI now manages the image; manual `-var image=` no longer needed)

## Steps

- [ ] **Step 1: `Dockerfile`** (uv-based, slim)
```dockerfile
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN uv pip install --system --no-cache .
# entrypoint is the CLI; Cloud Run service/job override `command`
ENTRYPOINT ["bellweather"]
CMD ["api", "--port", "8080"]
```

- [ ] **Step 2: `.dockerignore`** — `tests/`, `infra/`, `docs/`, `.git/`, `.venv/`, `__pycache__/`, `*.tfstate*`.

- [ ] **Step 3: `.github/workflows/deploy.yml`**
```yaml
name: deploy
on:
  push: { branches: [main] }
permissions: { contents: read, id-token: write }
env:
  REGION: us-central1
  REPO: bellweather
  SERVICE: bellweather-api
  JOB: bellweather-worker
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.GCP_WIF_PROVIDER }}
          service_account: ${{ secrets.GCP_DEPLOYER_SA }}
      - uses: google-github-actions/setup-gcloud@v2
      - name: Configure docker
        run: gcloud auth configure-docker $REGION-docker.pkg.dev --quiet
      - name: Build & push
        run: |
          IMAGE="$REGION-docker.pkg.dev/${{ secrets.GCP_PROJECT }}/$REPO/app:${{ github.sha }}"
          docker build -t "$IMAGE" .
          docker push "$IMAGE"
          echo "IMAGE=$IMAGE" >> "$GITHUB_ENV"
      - name: Run migrations (one-off job execution)
        run: |
          gcloud run jobs deploy bellweather-migrate \
            --image "$IMAGE" --region "$REGION" \
            --service-account "${{ secrets.GCP_RUNTIME_SA }}" \
            --set-cloudsql-instances "${{ secrets.GCP_SQL_CONN }}" \
            --set-secrets DATABASE_URL=bellweather-database-url:latest \
            --set-env-vars BELLWEATHER_BUCKET=${{ secrets.GCP_BUCKET }},BELLWEATHER_OBS_BUCKET=hour \
            --command bellweather --args migrate --quiet
          gcloud run jobs execute bellweather-migrate --region "$REGION" --wait
      - name: Deploy API
        run: gcloud run services update $SERVICE --region "$REGION" --image "$IMAGE"
      - name: Deploy worker job
        run: gcloud run jobs update $JOB --region "$REGION" --image "$IMAGE"
```

- [ ] **Step 4: Document required GitHub secrets** in `infra/README.md`:
  `GCP_PROJECT`, `GCP_BUCKET`, `GCP_SQL_CONN` (the `sql_connection` output), `GCP_RUNTIME_SA` (bellweather-runtime email), `GCP_DEPLOYER_SA`, `GCP_WIF_PROVIDER`. Include the gcloud commands to create a Workload Identity Federation pool/provider + a deployer SA with roles `run.admin`, `artifactregistry.writer`, `iam.serviceAccountUser`, `cloudsql.client`. (Simpler-but-less-secure alternative: a deployer SA JSON key in `GCP_SA_KEY` with `google-github-actions/auth` `credentials_json` — note this option.)

- [ ] **Step 5: Validate workflow yaml** locally (`yamllint` optional) and confirm `Dockerfile` builds:
Run: `docker build -t bellweather:test .`
Expected: image builds; `docker run --rm bellweather:test --help` prints CLI help.

- [ ] **Step 6: Commit** (`ci: add Dockerfile and deploy pipeline`).

## Acceptance criteria
- `docker build .` succeeds; `bellweather --help` runs in the container.
- `deploy.yml` builds+pushes a SHA-tagged image, runs the migrate job, and updates the API service + worker job.
- Required secrets + WIF setup (and the JSON-key fallback) are documented in `infra/README.md`.
- `make check` stays green (no app code changed; this is packaging/CI).
