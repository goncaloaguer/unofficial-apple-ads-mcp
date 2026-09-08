# Minimal, non-root container for Cloud Run.
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . uvicorn

# Non-root user; no writable filesystem needed.
RUN useradd --create-home --uid 10001 appuser
USER appuser

ENV MCP_TRANSPORT=http
# Cloud Run injects PORT; the app reads it.
EXPOSE 8080
CMD ["apple-ads-mcp"]
