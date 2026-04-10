FROM public.ecr.aws/lambda/python:3.13

# Install the shared common package (non-editable for Docker).
COPY common/ /common/
RUN python -m pip install --no-cache-dir /common

# Install remaining dependencies (filter out the editable common install).
COPY requirements.txt .
RUN grep -v '^\-e' requirements.txt > requirements-docker.txt \
    && python -m pip install --no-cache-dir -r requirements-docker.txt

COPY config.py exceptions.py logger.py main.py ${LAMBDA_TASK_ROOT}/
COPY core ${LAMBDA_TASK_ROOT}/core/

CMD ["main.lambda_handler"]
