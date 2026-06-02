FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1

# Caddy — a single static binary; the in-image reverse proxy that fronts
# FastAPI + Streamlit on the one Cloud Run ingress port (T17).
ARG CADDY_VERSION=2.8.4
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
      | tar -xz -C /usr/local/bin caddy \
 && caddy version \
 && apt-get purge -y curl \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
# Bake the collector-scripts repo (template manifests + scripts) into the image so the
# orchestrator can discover and spawn them (T27). BELLWEATHER_TEMPLATES_DIR points the
# template registry (templates.discover_templates) at this baked-in dir; default is the
# repo's own `producers/`, so the demo runs without an external repo (design §7).
COPY producers ./producers
ENV BELLWEATHER_TEMPLATES_DIR=/app/producers
# Install the pipeline AND the `ui` group: Streamlit is a RUNTIME dependency of
# this combined image (it serves the operator UI), not just a local-dev tool.
RUN uv pip install --system --no-cache --group ui .

COPY Caddyfile ./Caddyfile
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Default entrypoint: the combined UI+API supervisor (the Cloud Run service).
# The worker + migrate Cloud Run Jobs override `command` to run the CLI directly
# (`bellweather worker --once` / `bellweather migrate`), bypassing this script.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
