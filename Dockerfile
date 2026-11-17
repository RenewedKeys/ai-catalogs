FROM python:3.15-alpine

WORKDIR /app

# Build dependencies for packages without musl wheels
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev \
    su-exec

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# /data holds durable state. The container starts as root only so the entrypoint can give
# /data to PUID:PGID (default 1000:1000, the appuser below), then runs the service as that
# user; the CLI drops root the same way (app/core/privileges.py). Like linuxserver images.
RUN chmod +x /app/entrypoint.sh \
    && adduser -D -u 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

EXPOSE 8000

# Web health check. The worker service overrides it in docker-compose.yml.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import os, httpx; httpx.get('http://localhost:' + os.environ.get('PORT', '8000') + '/healthz', timeout=5).raise_for_status()"

# With no arguments the entrypoint starts the web server; with arguments it runs them.
ENTRYPOINT ["/app/entrypoint.sh"]
