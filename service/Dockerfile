FROM python:3.13-alpine AS builder

WORKDIR /app

# Install build dependencies for Python packages with C extensions
RUN apk add --no-cache gcc musl-dev libffi-dev openssl-dev cargo

# Install dependencies in builder stage
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --target=/app/packages -r requirements.txt

# Final stage
FROM python:3.13-alpine

WORKDIR /app

# Install runtime dependencies only
RUN apk add --no-cache libffi openssl

# Lambda Web Adapter exposes this containerized Flask app through Lambda + Function URLs.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

# Copy installed packages from builder
COPY --from=builder /app/packages /app/packages

# Copy only runtime application code
COPY app.py config.py logger.py sync.py tenant_data_repository.py xero_repository.py ./
COPY core/ ./core/
COPY templates/ ./templates/
COPY static/ ./static/
COPY utils/ ./utils/

# Pre-compile Python bytecode for faster cold starts
# RUN python3.13 -m compileall -q .

# Set Python path to include packages
ENV PYTHONPATH=/app/packages
ENV PORT=8080
ENV AWS_LWA_PORT=8080
# Reduce Python startup overhead
ENV PYTHONUNBUFFERED=1
# Flask app location
ENV FLASK_APP=app.py

EXPOSE 8080

# Flask dev server is optimal for Lambda Function URLs (single concurrent request per container)
# Gunicorn would add unnecessary overhead (worker management, pre-fork) for Lambda's execution model
CMD ["python", "-m", "flask", "run", "--host", "0.0.0.0", "--port", "8080"]
