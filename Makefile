.PHONY: dev up down check test lint fmt migrate ui
dev:    ; uv sync
ui:     ; uv run --group ui streamlit run src/bellweather/web/app.py
up:     ; docker compose up -d
down:   ; docker compose down -v
lint:   ; uv run ruff check .
fmt:    ; uv run ruff format .
test:   ; uv run pytest
check:  ; uv run ruff check . && uv run ruff format --check . && uv run pytest
migrate:; uv run bellweather migrate
