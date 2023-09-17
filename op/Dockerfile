FROM python:3.7
RUN pip install kopf kubernetes requests prometheus_client
COPY op.py /op.py
CMD kopf run --standalone -A /op.py