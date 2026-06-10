# ── Builder Stage ────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies in a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy pyproject.toml to install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# ── Final Runtime Stage ──────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy package source code and assets
COPY polyagent/ polyagent/
COPY dashboard/ dashboard/

# Expose FastAPI default port
EXPOSE 8000

# Run FastAPI application via uvicorn
CMD ["uvicorn", "polyagent.main:app", "--host", "0.0.0.0", "--port", "8000"]
