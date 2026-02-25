FROM python:3.13-slim

WORKDIR /app

# Lambda Web Adapter exposes this containerized Flask app through Lambda + Function URLs.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
ENV AWS_LWA_PORT=8080

EXPOSE 8080

CMD ["python3.13", "-c", "import os; from app import app; app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))"]
