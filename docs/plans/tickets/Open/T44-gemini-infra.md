# T44 — Gemini key infra + conditional secret baseline (fixes empty-payload apply failure)

**Spec:** `docs/specs/2026-06-03-gemini-llm-provider-design.md` (D4 worker-only key posture, D5 conditional baseline).
**Depends on:** T43 (code reads `GEMINI_API_KEY`), T42 (the Anthropic secret trio this mirrors). **Branch:** `ticket/T44-gemini-infra`. **PR, do not merge without approval.**

## Goal
Wire `GEMINI_API_KEY` into GCP exactly like the Anthropic key (T42): a `bellweather-gemini-api-key` Secret Manager secret fed from a new optional `var.gemini_api_key`, a `secretAccessor` grant to the **runtime SA only** (the orchestrator SA stays excluded — spawned templates must not reach the key via ambient ADC, K1/K4), and an env mount on the **worker Job only** (never the public API service — the preview route stays key-less in prod, K10). Simultaneously fix the bug that broke the 2026-06-03 `terraform apply`: GCP rejects a `google_secret_manager_secret_version` with an empty payload, so the "leave the key var empty to apply the baseline" promise (`variables.tf`) never worked — the apply died on `google_secret_manager_secret_version.anthropic_key` and skipped the worker Job + its drain scheduler. Both providers' `_version` resources AND their worker-Job env mounts become conditional on a non-empty var. Corollary (document it): **the tfvar is the source of truth** — enabling a key later means setting the var and re-applying (which creates the version *and* mounts the env var together), not hand-adding a secret version with `gcloud`. An unset key degrades per-provider only: `extract()` raises a clear `RuntimeError`; fetch/ingest/GDELT and the other provider are unaffected. No GitHub secrets change; `deploy.yml` is untouched.

## Files
- Modify: `infra/variables.tf` — add `gemini_api_key` (string, default `""`, sensitive); reword `anthropic_api_key`'s description (the "drop the key into the secret later" guidance is wrong under the conditional baseline).
- Modify: `infra/main.tf` — gemini secret trio (secret + conditional version + runtime-SA grant); conditional `count` on the **anthropic** version; `dynamic "env"` for both keys on the worker Job; widen the T42 comment block to cover both providers.
- Modify: `infra/README.md` — document the gemini secret, the tfvar-is-source-of-truth rule, and the fixed optional-key semantics.

## Interface
Copied verbatim from the plan's "Locked interfaces" (`docs/plans/2026-06-03-gemini-llm-provider.md`).

**variables.tf:**
```hcl
variable "gemini_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Google AI Studio key for the Gemini extraction provider (worker Job only) — stored in Secret Manager. Optional: leave empty and no secret version or env mount is created (the Gemini path raises until it is set); to enable later, set this var and re-apply."
}
```

**main.tf** — the conditional pattern (applies to BOTH providers):
```hcl
resource "google_secret_manager_secret_version" "gemini_key" {
  count       = var.gemini_api_key == "" ? 0 : 1
  secret      = google_secret_manager_secret.gemini_key.id
  secret_data = var.gemini_api_key
}
```
and on the worker Job's container (replacing the unconditional `env { name = "ANTHROPIC_API_KEY" … }` block, plus the gemini twin):
```hcl
dynamic "env" {
  for_each = var.gemini_api_key == "" ? [] : [1]
  content {
    name = "GEMINI_API_KEY"
    value_source {
      secret_key_ref {
        secret  = google_secret_manager_secret.gemini_key.secret_id
        version = "latest"
      }
    }
  }
}
```

## Steps

> **No Python, no tests:** this ticket is Terraform + docs only. The gate is `terraform validate` + `terraform plan` reviewed by hand (CI does not run Terraform).

- [ ] **Step 1: `variables.tf`.** Add `gemini_api_key` (snippet above); rewrite `anthropic_api_key`'s description to match the new semantics ("leave empty and no secret version or env mount is created; set the var and re-apply to enable").

- [ ] **Step 2: gemini secret trio in `main.tf`.** After the Anthropic block (`infra/main.tf:90-126`), add `google_secret_manager_secret.gemini_key` (`secret_id = "bellweather-gemini-api-key"`, `replication { auto {} }`, `depends_on = [google_project_service.apis]`), the conditional `_version` (snippet above), and `google_secret_manager_secret_iam_member.gemini_key_access` granting `roles/secretmanager.secretAccessor` to `google_service_account.runtime` only. Extend the T42 comment block: same posture for both LLM keys (worker-only env; orchestrator SA excluded; API service never mounts either key).

- [ ] **Step 3: make the anthropic version conditional.** `count = var.anthropic_api_key == "" ? 0 : 1` on `google_secret_manager_secret_version.anthropic_key` (`main.tf:113`). The worker Job's `depends_on` entries keep referencing the resource by address — that stays valid at `count = 0`.

- [ ] **Step 4: worker Job env mounts.** Replace the unconditional `ANTHROPIC_API_KEY` `env` block (`main.tf:245-253`) with the `dynamic "env"` form gated on `var.anthropic_api_key != ""`, and add the `GEMINI_API_KEY` twin gated on `var.gemini_api_key != ""`. Append `google_secret_manager_secret_version.gemini_key` + `google_secret_manager_secret_iam_member.gemini_key_access` to the Job's `depends_on`. **Do NOT touch the API service or orchestrator Job blocks** — neither mounts either key, by design.

- [ ] **Step 5: validate + plan.** `terraform validate`, then `terraform plan` twice and eyeball:
  - with both key vars **empty**: plan creates the gemini secret + grant, **no** `_version` resources, and the worker Job's plan shows **no** `ANTHROPIC_API_KEY`/`GEMINI_API_KEY` env (this is the repro of the 2026-06-03 failure — it must now plan clean);
  - with `-var gemini_api_key=test`: plan adds exactly the gemini `_version` + the worker env mount.

- [ ] **Step 6: README.** In `infra/README.md`, retitle "The LLM scrape engine secret (T42)" coverage to span both keys: add a short gemini paragraph (free tier, worker-only, orchestrator excluded — same table), state the tfvar-is-source-of-truth rule, and **delete** the stale "drop the real key into the bellweather-anthropic-api-key secret later" sentence (under the conditional baseline a hand-added version never gets an env mount).

- [ ] **Step 7: Commit** (`feat(infra): GEMINI_API_KEY secret trio (worker-only) + conditional secret baseline for both LLM keys`).

- [ ] **Step 8 (deploy-time, not CI): apply.** `terraform apply` with the real keys in `terraform.tfvars`. Note for the operator: this is also what completes the half-applied 2026-06-03 baseline (the missing worker Job + `bellweather-worker-drain` scheduler get created), and the first CI deploy afterward replaces the placeholder `hello` image everywhere.

## Acceptance criteria
- `terraform validate` passes; `terraform plan` with both key vars empty plans **zero** `google_secret_manager_secret_version` resources for the LLM keys and **no** LLM-key env on the worker Job — and does not error (the 2026-06-03 empty-payload failure mode is gone).
- With a non-empty `gemini_api_key`: the plan shows the `bellweather-gemini-api-key` version + the worker Job's `GEMINI_API_KEY` env from `secret_key_ref` (`latest`).
- The `secretAccessor` grant on the gemini secret names the runtime SA **only**; the orchestrator SA has no grant; the API service template contains **no** `GEMINI_API_KEY`/`ANTHROPIC_API_KEY` mounts.
- No changes to `deploy.yml`, GitHub secrets, outputs, or any Cloud Run resource other than the worker Job's env.
- `infra/README.md` documents the gemini secret + the tfvar-is-source-of-truth rule and no longer recommends hand-adding secret versions.
