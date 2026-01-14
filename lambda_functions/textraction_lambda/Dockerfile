FROM public.ecr.aws/lambda/python:3.13

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY config.py exceptions.py main.py ${LAMBDA_TASK_ROOT}/
COPY core ${LAMBDA_TASK_ROOT}/core/

CMD ["main.lambda_handler"]
