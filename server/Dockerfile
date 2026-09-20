# server/Dockerfile — Soulscape hub image (issue #37: sim + api processes).
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1

# Copy root workspace files
COPY pyproject.toml uv.lock ./

# Copy member definitions
COPY server/pyproject.toml ./server/
COPY client/pyproject.toml ./client/

# Install dependencies for the server package (no dev deps)
RUN uv sync --frozen --no-dev --package server --no-install-project

# Second stage
FROM python:3.13-slim-bookworm

WORKDIR /app

# Copy virtual environment
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy the full server package and the shared models it imports.
# (An earlier revision copied an obsolete file subset and missed new
# modules; copying the whole tree keeps the image in sync with the repo.)
COPY server/ ./server/
COPY shared/ ./shared/

# /app on the path so `server` and `shared` import as top-level packages.
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1

# Default: run the API. The sim service overrides CMD (see docker-compose.yml).
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "9785"]
