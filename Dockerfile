FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN pip install --no-cache-dir google-cloud-storage>=2.16.0

COPY serve.py index.html gate.html feedback.html /app/
COPY assets/ /app/assets/

EXPOSE 8080

CMD ["python3", "/app/serve.py"]
