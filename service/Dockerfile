FROM python:3.13-slim AS builder

WORKDIR /app

# Install only essential build dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install the shared common package first (non-editable for Docker).
COPY common/ /common/
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --target=/app/packages /common

# Install remaining dependencies (filter out the editable common install
# since we already installed it non-editable above).
COPY service/requirements.txt ./
RUN grep -v '^\-e' requirements.txt > requirements-docker.txt \
    && pip install --no-cache-dir --target=/app/packages -r requirements-docker.txt

# Final stage
#
# Deliberate: this image runs as root.  Adding a non-root user (USER appuser)
# would require reworking nginx port binding, file permissions, and the Valkey
# privilege drop in start.sh.  The security benefit is marginal given the App
# Runner deployment model, and other project Dockerfiles (numerint, gwd-hub)
# also run as root.
FROM python:3.13-slim

WORKDIR /app

# Install Valkey, Nginx, and curl (for readiness checks).
RUN apt-get update && apt-get install -y --no-install-recommends \
    valkey \
    nginx \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder.
COPY --from=builder /app/packages /usr/local/lib/python3.13/site-packages

# Copy only runtime application code.
# Copy every top-level Python module so new service modules are not accidentally
# omitted from the container image during future refactors.
COPY service/*.py ./
COPY service/content/ ./content/
COPY service/core/ ./core/
COPY service/templates/ ./templates/
# Keep static assets in all images for environment parity and direct Function URL fallback.
# CloudFront serves /static/* in production; removing static saves only ~0.15 MB, so we optimize for simplicity.
COPY service/static/ ./static/
COPY service/ui/ ./ui/
COPY service/routes/ ./routes/
COPY service/utils/ ./utils/
COPY service/valkey.conf /etc/valkey/valkey.conf
COPY service/nginx.conf /app/nginx.conf
COPY service/nginx-routes.conf /etc/nginx/nginx-routes.conf
COPY service/start.sh /start.sh
RUN chmod +x /start.sh

# Pre-compile Python bytecode for faster cold starts.
RUN python3.13 -m compileall -q .

EXPOSE 8080

CMD ["/start.sh"]
