# Working agreement for autonomous workers

- Implement ONE ticket per branch: `ticket/T<NN>-<slug>`. Open ONE PR. NEVER merge to `main`.
- TDD: write a failing test, run it, write minimal code, make it pass, commit. Repeat.
- The gate is `make check` (ruff lint + format-check + pytest). A ticket is done only when it is green.
- Conventional commits (`feat:`, `test:`, `chore:`, `fix:`), small and frequent.
- Local stack: `make up` (Postgres + fake GCS), then `make migrate`. `make down` to reset.
- Config is env-driven via `src/bellweather/config.py`; never hardcode secrets. Copy `.env.example` to `.env`.
- If a ticket needs something unbuilt, STOP and do its prerequisite ticket first — do not stub past it.
- Each ticket file in `docs/superpowers/plans/tickets/` is the source of truth for its task.
