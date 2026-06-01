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
