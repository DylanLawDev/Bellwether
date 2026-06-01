.PHONY: dev up down check test lint fmt migrate
dev:    ; uv sync
up:     ; docker compose up -d
down:   ; docker compose down -v
lint:   ; uv run ruff check .
fmt:    ; uv run ruff format .
test:   ; uv run pytest
check:  ; uv run ruff check . && uv run ruff format --check . && uv run pytest
migrate:; uv run bellweather migrate
