# ---- Builder Stage: install Python dependencies ----
FROM python:3.12-slim AS builder

WORKDIR /app

# Install dependencies into an isolated virtual environment.
# All packages ship pre-built wheels — no system build tools needed.
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# ---- Runtime Stage: minimal production image ----
FROM python:3.12-slim AS runtime

# Create a non-root user for the application process.
RUN groupadd --system gateway && \
    useradd --system --no-log-init --gid gateway --create-home gateway

# Install curl (used by HEALTHCHECK).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Copy the virtual environment from the builder stage.
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy application source (production entry point + all packages).
COPY app/ /app/app/

# Switch to the non-root gateway user.
USER gateway

# Verify the application can be imported at build time.
RUN python -c "from app.main import app; print(f'Gateway {app.title} v{app.version} ready')"

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
