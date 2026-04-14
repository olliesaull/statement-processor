FROM public.ecr.aws/lambda/python:3.13

# Install the shared common package (non-editable for Docker).
COPY common/ /common/
RUN python -m pip install --no-cache-dir /common

# Install remaining dependencies (filter out the editable common install).
COPY lambda_functions/extraction_lambda/requirements.txt .
RUN grep -v '^\-e' requirements.txt > requirements-docker.txt \
    && python -m pip install --no-cache-dir -r requirements-docker.txt

COPY lambda_functions/extraction_lambda/config.py lambda_functions/extraction_lambda/logger.py lambda_functions/extraction_lambda/main.py ${LAMBDA_TASK_ROOT}/
COPY lambda_functions/extraction_lambda/core ${LAMBDA_TASK_ROOT}/core/

CMD ["main.lambda_handler"]
