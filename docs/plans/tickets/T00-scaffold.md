# T00 — Repo scaffold, tooling, CI, local docker-compose, AGENTS.md

**Spec:** `docs/specs/2026-05-31-ingestor-extractor-design.md`
**Plan:** `docs/plans/2026-05-31-ingestor-extractor.md`
**Depends on:** none. **Branch:** `ticket/T00-scaffold`. **PR, do not merge without approval.**

## Goal
Stand up the Python project skeleton so every later ticket has tooling, tests, lint, CI, and a local stack. Prove the pipeline with one trivial passing test.

## Files
- Create: `pyproject.toml`, `Makefile`, `docker-compose.yml`, `AGENTS.md`, `.gitignore`, `.env.example`
- Create: `src/bellweather/__init__.py`, `src/bellweather/version.py`
- Create: `.github/workflows/ci.yml`
- Create: `tests/test_smoke.py`

## Conventions this ticket establishes
Python 3.12, `uv` for env/deps, `src/` layout, `ruff` lint+format, `pytest`. `make check` is the single gate.

## Steps

- [ ] **Step 1: `pyproject.toml`** (uv-managed)
```toml
[project]
name = "bellweather"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "psycopg[binary,pool]>=3.2",
  "google-cloud-storage>=2.18",
  "pydantic>=2.9",
  "pydantic-settings>=2.6",
  "httpx>=0.27",
  "typer>=0.12",
]

[dependency-groups]
dev = ["pytest>=8.3", "ruff>=0.7", "pytest-httpserver>=1.1"]

[project.scripts]
bellweather = "bellweather.cli:app"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/bellweather"]

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.pytest.ini_options]
addopts = "-q"
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 2: `Makefile`**
```make
.PHONY: dev up down check test lint fmt migrate
dev:    ; uv sync
up:     ; docker compose up -d
down:   ; docker compose down -v
lint:   ; uv run ruff check .
fmt:    ; uv run ruff format .
test:   ; uv run pytest
check:  ; uv run ruff check . && uv run ruff format --check . && uv run pytest
migrate:; uv run bellweather migrate
```

- [ ] **Step 3: `docker-compose.yml`** (local Postgres + fake GCS)
```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: bellweather
      POSTGRES_PASSWORD: bellweather
      POSTGRES_DB: bellweather
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bellweather"]
      interval: 2s
      timeout: 3s
      retries: 20
  gcs:
    image: fsouza/fake-gcs-server:1.49
    command: ["-scheme", "http", "-port", "4443", "-public-host", "localhost:4443"]
    ports: ["4443:4443"]
```

- [ ] **Step 4: `.env.example`** (copy to `.env` for local; documents every env var)
```bash
DATABASE_URL=postgresql://bellweather:bellweather@localhost:5432/bellweather
BELLWEATHER_BUCKET=bellweather-bronze-local
STORAGE_EMULATOR_HOST=http://localhost:4443   # unset in prod to use real GCS
BELLWEATHER_API_URL=http://localhost:8000
BELLWEATHER_OBS_BUCKET=hour                    # hour | 15min
```

- [ ] **Step 5: `.gitignore`** — include `.venv/`, `.env`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `infra/.terraform/`, `*.tfstate*`.

- [ ] **Step 6: `src/bellweather/__init__.py`** (empty) and `version.py`
```python
__version__ = "0.1.0"
```

- [ ] **Step 7: Write the smoke test** `tests/test_smoke.py`
```python
from bellweather.version import __version__

def test_version_present():
    assert __version__ == "0.1.0"
```

- [ ] **Step 8: `AGENTS.md`** — conventions every autonomous worker reads first
```markdown
# Working agreement for autonomous workers

- Implement ONE ticket per branch: `ticket/T<NN>-<slug>`. Open ONE PR. NEVER merge to `main`.
- TDD: write a failing test, run it, write minimal code, make it pass, commit. Repeat.
- The gate is `make check` (ruff lint + format-check + pytest). A ticket is done only when it is green.
- Conventional commits (`feat:`, `test:`, `chore:`, `fix:`), small and frequent.
- Local stack: `make up` (Postgres + fake GCS), then `make migrate`. `make down` to reset.
- Config is env-driven via `src/bellweather/config.py`; never hardcode secrets. Copy `.env.example` to `.env`.
- If a ticket needs something unbuilt, STOP and do its prerequisite ticket first — do not stub past it.
- Each ticket file in `docs/plans/tickets/` is the source of truth for its task.
```

- [ ] **Step 9: `.github/workflows/ci.yml`** — run the gate on PRs, with service containers
```yaml
name: ci
on:
  pull_request:
  push: { branches: [main] }
jobs:
  check:
    runs-on: ubuntu-latest
    services:
      db:
        image: postgres:16
        env: { POSTGRES_USER: bellweather, POSTGRES_PASSWORD: bellweather, POSTGRES_DB: bellweather }
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U bellweather" --health-interval 2s
          --health-timeout 3s --health-retries 20
      gcs:
        image: fsouza/fake-gcs-server:1.49
        ports: ["4443:4443"]
        options: --entrypoint /bin/fake-gcs-server
    env:
      DATABASE_URL: postgresql://bellweather:bellweather@localhost:5432/bellweather
      BELLWEATHER_BUCKET: bellweather-bronze-ci
      STORAGE_EMULATOR_HOST: http://localhost:4443
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run ruff check . && uv run ruff format --check .
      - run: uv run pytest
```
> Note: the `gcs` service `--entrypoint` override is finicky in Actions; if it fails, a later ticket may start fake-gcs inside the test session instead. Leave a TODO comment but keep CI green by ensuring tests that need GCS are skipped when `STORAGE_EMULATOR_HOST` is unreachable (T03 handles this).

- [ ] **Step 10: Run the gate**
Run: `make dev && make check`
Expected: ruff passes, `test_version_present` PASSES.

- [ ] **Step 11: Commit & open PR**
```bash
git add -A && git commit -m "chore: scaffold project, tooling, CI, local stack"
```

## Acceptance criteria
- `make check` is green locally and in CI.
- `make up` brings up Postgres + fake-gcs; `make down` tears them down.
- `AGENTS.md` exists and documents the working agreement.
- `bellweather` console script is declared (its `cli:app` is implemented in T07).
  > To keep T00 green before `cli.py` exists, you MAY temporarily add a stub `src/bellweather/cli.py` with `import typer; app = typer.Typer()`. T07 fills it in.
