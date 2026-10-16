# Aasaan: one small container. SQLite means ONE app instance only (do not scale this service horizontally).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    AASAAN_DB=/app/data/aasaan.db AASAAN_UPLOADS=/app/data/uploads

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend backend
COPY frontend frontend
COPY config config

# Run as a non-root user; /app/data is the mounted volume (database + uploads)
RUN useradd --create-home --uid 10001 aasaan && mkdir -p /app/data && chown -R aasaan /app
USER aasaan
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# one worker process: the job worker and SQLite both assume a single instance
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
