# server/Dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1

# Copy root workspace files
COPY pyproject.toml uv.lock ./

# Copy member definitions
COPY server/pyproject.toml ./server/
COPY client/pyproject.toml ./client/

# Install dependencies for the package named "server"
# Note: This requires your uv.lock to be up-to-date with the name change!
RUN uv sync --frozen --no-dev --package server --no-install-project

# Second stage
FROM python:3.13-slim-bookworm

WORKDIR /app

# Copy virtual environment
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Create the package directory and copy files into it
# This results in /app/server/main.py, /app/server/database.py, etc.
WORKDIR /app/server
COPY server/main.py server/database.py server/managers.py server/models.py server/security.py ./
COPY server/routers ./routers
COPY server/pyproject.toml ./
# Copy .env if needed, though docker-compose usually handles this
# COPY server/.env ./

# Return to root
WORKDIR /app

# Add /app to PYTHONPATH so "server" is recognized as a top-level package
ENV PYTHONPATH="/app"

# Run the app as a module
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "9785"]