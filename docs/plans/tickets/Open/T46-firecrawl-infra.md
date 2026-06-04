# T46 — Firecrawl key infra (orchestrator-SA secret trio + Job env mount + comment surgery)

**Spec:** `docs/specs/2026-06-03-firecrawl-fetch-adapter-design.md` (D6 key posture).
**Plan:** `docs/plans/2026-06-03-firecrawl-fetch-adapter.md`.
**Depends on:** T45 (the code that reads `FIRECRAWL_API_KEY` + the `_child_env` allowlist that justifies the grant), T44 (the conditional-secret baseline this copies). **Branch:** `ticket/T46-firecrawl-infra`. **PR, do not merge without approval.**

## Goal

Wire `FIRECRAWL_API_KEY` into GCP using T44's conditional pattern, with one deliberate difference from the LLM keys: the secret is granted to and mounted on the **orchestrator** (SA + Cloud Run Job), not the runtime SA — because the consumer is the scrape collector the orchestrator spawns (design D6 / scrape-engine D-e). The API service and worker Job get **nothing**: a prod preview of a firecrawl spec degrades into the same graceful keyless `RuntimeError` the LLM path already has. This grant intentionally relaxes the orchestrator-SA's "only the DB-URL secret" posture, and the safety argument moves into code: `orchestrator._TEMPLATE_EXTRA_ENV` (T45) forwards the key only to the first-party `scrape` template, so arbitrary external templates still cannot reach it. The `main.tf` comments documenting the old posture must be **rewritten, not appended to**.

## Files

- Modify: `infra/variables.tf` — `firecrawl_api_key` (string, default `""`, sensitive); description follows the T44 wording ("leave empty and no secret version or env mount is created; set the var and re-apply to enable").
- Modify: `infra/main.tf` — firecrawl secret trio + `firecrawl_key_set` local + orchestrator-Job `dynamic "env"` + `depends_on` additions + orchestrator-SA comment rewrite.
- Modify: `infra/README.md` — document the secret, **who holds it and why it differs from the LLM keys**, tfvar-is-source-of-truth.

## Interface

Copied verbatim from the plan's "Locked interfaces" (`docs/plans/2026-06-03-firecrawl-fetch-adapter.md`):

```hcl
locals {
  firecrawl_key_set = nonsensitive(var.firecrawl_api_key != "")
}
resource "google_secret_manager_secret_version" "firecrawl_key" {
  count       = local.firecrawl_key_set ? 1 : 0
  secret      = google_secret_manager_secret.firecrawl_key.id
  secret_data = var.firecrawl_api_key
}
resource "google_secret_manager_secret_iam_member" "firecrawl_key_access" {
  secret_id = google_secret_manager_secret.firecrawl_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator.email}"
}
# orchestrator Job container:
dynamic "env" {
  for_each = local.firecrawl_key_set ? [1] : []
  content {
    name = "FIRECRAWL_API_KEY"
    value_source {
      secret_key_ref {
        secret  = google_secret_manager_secret.firecrawl_key.secret_id
        version = "latest"
      }
    }
  }
}
```

Secret id: `bellweather-firecrawl-api-key` (`replication { auto {} }`, `depends_on = [google_project_service.apis]`).

## Steps

> **No Python, no tests:** Terraform + docs only. The gate is `terraform validate` + `terraform plan` reviewed by hand (CI does not run Terraform).

- [ ] **Step 1: `variables.tf`.** Add `firecrawl_api_key` (snippet above).

- [ ] **Step 2: secret trio in `main.tf`.** After the gemini block, add `google_secret_manager_secret.firecrawl_key`, the conditional `_version`, and the `secretAccessor` grant to **`google_service_account.orchestrator`**. Add `firecrawl_key_set` to the existing presence-flag `locals` block (same `nonsensitive()` rationale comment applies).

- [ ] **Step 3: comment surgery.** Rewrite the orchestrator-SA comment block (the one explaining it is granted ONLY the DATABASE_URL secret because any secret it can read is effectively reachable by an external template): the firecrawl key is the **D-e scoped exception** — reachable by the orchestrator, but forwarded by `orchestrator._TEMPLATE_EXTRA_ENV` to the first-party `scrape` template only; point the comment at that constant. Also extend the LLM-key comment block's "who holds what" summary so all three vendor keys read as one posture table: LLM keys → worker only; fetch key → orchestrator only; API service → none.

- [ ] **Step 4: orchestrator Job env mount.** Add the `dynamic "env"` block to the orchestrator Job's container and append `google_secret_manager_secret_version.firecrawl_key` + `google_secret_manager_secret_iam_member.firecrawl_key_access` to its `depends_on`. **Do NOT touch the API service or worker Job blocks.**

- [ ] **Step 5: validate + plan.** `terraform validate`; `terraform plan` with the var empty (secret + grant created, **no** `_version`, **no** orchestrator env mount — plans clean per the T44 baseline fix) and with `-var firecrawl_api_key=test` (exactly the `_version` + the orchestrator-Job env appear).

- [ ] **Step 6: README.** Document the secret in `infra/README.md` next to the LLM keys: free tier ($0 — 1,000 credits/mo covers v0 scheduled volume), orchestrator-only mount, the `_TEMPLATE_EXTRA_ENV` forwarding rule, tfvar-is-source-of-truth.

- [ ] **Step 7: Commit** (`feat(infra): FIRECRAWL_API_KEY secret trio on the orchestrator (D-e scoped exception)`).

- [ ] **Step 8 (deploy-time, not CI): apply.** `terraform apply` with the real key in `terraform.tfvars` once a spec actually selects the firecrawl adapter — until then the empty-var baseline is a no-op.

## Acceptance criteria

- `terraform validate` passes; the empty-var plan creates the secret + grant but **zero** `_version` resources and **no** `FIRECRAWL_API_KEY` env anywhere; the set-var plan adds exactly the `_version` + the orchestrator-Job env mount.
- The grant names the **orchestrator SA only**; the runtime SA has no grant on this secret; the API service and worker Job templates contain no `FIRECRAWL_API_KEY` mount.
- The orchestrator-SA comment no longer claims "only the DB-URL secret" — it documents the D-e exception and points at `orchestrator._TEMPLATE_EXTRA_ENV`.
- No changes to `deploy.yml`, GitHub secrets, outputs, or any Cloud Run resource other than the orchestrator Job's env.
- `infra/README.md` covers the new secret, its distinct holder, and the tfvar rule.
