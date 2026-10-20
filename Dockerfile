FROM python:3.12.15-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends make \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    CAMPUSRELAY_DB=/data/campusrelay.sqlite3

WORKDIR /app
COPY app.py backend_integrity.py index.html Makefile requirements.lock .python-version ./
COPY tests/ ./tests/
RUN mkdir -p /data

EXPOSE 8000
CMD ["python", "app.py"]
