FROM python:3.12-slim

WORKDIR /app

COPY aitrack.py README.md ./
COPY aitrack_core ./aitrack_core

ENV AITRACK_CONFIG_DIR=/data \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data

EXPOSE 8765

CMD ["python", "/app/aitrack.py", "serve", "--host", "0.0.0.0", "--port", "8765", "--db", "/data/server.db"]
