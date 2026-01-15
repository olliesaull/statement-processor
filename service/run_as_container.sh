#!/bin/bash

docker build -t statement-processor .

docker run -p 8080:8080 \
  -v ~/.aws:/root/.aws:ro \
  --env-file .env \
  statement-processor
