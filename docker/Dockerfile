# BharatMarket Analytics: the dashboard and the API in one image. Build from the PROJECT ROOT:
#     docker compose build            (or: docker build -f docker/Dockerfile -t bharatmarket-analytics .)
# Add Spark (Java 21 + PySpark, about 1.3 GB more): docker compose build --build-arg WITH_SPARK=1
# (Java 21, not 17: the python:3.11-slim base is Debian 13, which no longer packages Java 17; PySpark 4 supports 21.)

# ---------------------------------------------------------------- 1. build the dashboard (Node is not kept in the final image)
FROM node:24-slim AS dashboard
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- 2. the application
FROM python:3.11-slim AS app
ARG WITH_SPARK=0
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN if [ "$WITH_SPARK" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends openjdk-21-jre-headless && rm -rf /var/lib/apt/lists/*; \
    fi

COPY docker/requirements-docker.txt ./requirements-docker.txt
RUN pip install -r requirements-docker.txt \
    && if [ "$WITH_SPARK" = "1" ]; then pip install "pyspark>=4.0,<5.0"; fi

# Run as an ordinary user, never as root. /app/data and /app/logs are where files are written (mounted by docker-compose.yml).
RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /app/data /app/logs \
    && chown -R app:app /app/data /app/logs

COPY backend ./backend
COPY scripts ./scripts
COPY --from=dashboard /build/frontend/dist ./frontend/dist
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

USER app
EXPOSE 8000
# /api/health answers 503 when MongoDB is unreachable, so "healthy" means the API AND the database work.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
