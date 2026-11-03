FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data WEB_CONCURRENCY=2
WORKDIR /app
# System libraries the geospatial wheels (rasterio, GDAL) link against but don't bundle.
# Newer python:*-slim images no longer ship libexpat, so install it explicitly.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libexpat1 zlib1g \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-dev.txt ./
# Retries and a longer timeout make the ~250 MB of downloads survive slow or flaky connections.
RUN pip install --no-cache-dir --retries 10 --timeout 120 --upgrade pip \
 && pip install --no-cache-dir --retries 10 --timeout 120 --only-binary=:all: -r requirements-dev.txt
COPY src ./src
COPY api ./api
COPY tests ./tests
COPY run_pipeline.sh ./
RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips='*'"]
