FROM python:3.13-slim AS builder

WORKDIR /app

# Install only essential build dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies in builder stage.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --target=/app/packages -r requirements.txt

# Final stage
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
COPY ./*.py ./
COPY core/ ./core/
COPY templates/ ./templates/
# Keep static assets in all images for environment parity and direct Function URL fallback.
# CloudFront serves /static/* in production; removing static saves only ~0.15 MB, so we optimize for simplicity.
COPY static/ ./static/
COPY ui/ ./ui/
COPY utils/ ./utils/
COPY valkey.conf /etc/valkey/valkey.conf
COPY nginx.conf /app/nginx.conf
COPY nginx-routes.conf /etc/nginx/nginx-routes.conf
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Pre-compile Python bytecode for faster cold starts.
RUN python3.13 -m compileall -q .

EXPOSE 8080

CMD ["/start.sh"]
