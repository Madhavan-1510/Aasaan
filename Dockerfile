# Aasaan: one small container for Azure Container Apps / Docker.
# SQLite means ONE app instance only (do not scale this service horizontally).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AASAAN_DB=/app/data/aasaan.db \
    AASAAN_UPLOADS=/app/data/uploads

WORKDIR /app

# Create a dedicated non-root user and persistent storage directory
RUN useradd --create-home --uid 10001 aasaan \
    && mkdir -p /app/data/uploads \
    && chown -R aasaan:aasaan /app

# Copy dependency specifications first to leverage Docker layer caching
COPY --chown=aasaan:aasaan backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy application source code and configurations
COPY --chown=aasaan:aasaan backend backend
COPY --chown=aasaan:aasaan frontend frontend
COPY --chown=aasaan:aasaan config config

# Run as non-root user
USER aasaan

# SQLite database and user uploads volume
VOLUME ["/app/data"]

EXPOSE 8000

# Health check against the /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Start server bound to 0.0.0.0 on port 8000 using the FastAPI factory entry point
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
