FROM public.ecr.aws/lambda/python:3.13

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY config.py logger.py main.py ${LAMBDA_TASK_ROOT}/

CMD ["main.lambda_handler"]
