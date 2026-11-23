FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EXHIBIT_DB=/data/exhibits.sqlite3

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data \
    && useradd --create-home --uid 10001 --user-group appuser \
    && chown -R appuser:appuser /data /srv
USER appuser

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"

# Exactly one worker: the in-process submission gate enforces arrival order.
# SQLite remains the source of truth and keeps each replacement atomic.
# The app is built by a factory so importing the module never touches the DB.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
