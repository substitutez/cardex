# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ==============================================================================
# Stage 1: Build virtual environment with uv and install dependencies
# ==============================================================================
FROM python:3.11-slim AS builder

# Install uv package manager
RUN pip install --no-cache-dir uv==0.8.13

WORKDIR /build

# Create dedicated virtual environment
ENV VIRTUAL_ENV=/opt/venv
RUN uv venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy dependency manifests
COPY pyproject.toml README.md uv.lock* ./
COPY frontend/requirements.txt ./frontend-requirements.txt

# Install all dependencies into virtual environment using uv
RUN uv pip install --no-cache -r frontend-requirements.txt
RUN uv pip install --no-cache .

# ==============================================================================
# Stage 2: Minimal runtime image with non-root appuser
# ==============================================================================
FROM python:3.11-slim AS runner

# Install curl for container health check probes
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Security: Create non-root system user
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -m -s /bin/bash appuser

WORKDIR /code

# Copy pre-built virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv

# Configure environment variables
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Copy application, frontend, and static vehicle data with proper ownership
COPY --chown=appuser:appuser ./app ./app
COPY --chown=appuser:appuser ./frontend ./frontend
COPY --chown=appuser:appuser ./data ./data

# Container healthcheck hitting the /healthz probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

# Run container as non-root user
USER appuser

EXPOSE 8080

# Production entrypoint running Gunicorn with 4 Uvicorn workers
CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8080", "frontend.main:app"]
