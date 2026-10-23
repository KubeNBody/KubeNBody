# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
# =============================================================================
# Stage 1: Build dependencies
# =============================================================================
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libpq-dev \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# pinned, the same as in Dockerfile.gpu (Poetry 2 also reads a lock file
# written by Poetry 1.8)
RUN pip install --no-cache-dir poetry==2.4.1 \
    && poetry config virtualenvs.create false

WORKDIR /app

COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-interaction --no-ansi --no-root

# =============================================================================
# Stage 2: Production image
# =============================================================================
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
# uid/gid 999: the manifests' securityContext runs the pods as this user
RUN groupadd -r -g 999 nbody && useradd -r -u 999 -g nbody -d /app -s /sbin/nologin nbody

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --chown=nbody:nbody ./src ./src
COPY --chown=nbody:nbody ./migrations ./migrations

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src

USER nbody
