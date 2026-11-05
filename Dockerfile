# Image légère (HTTP seul) :      docker build -t dealarr .
# Image avec navigateur headless : docker build --target browser -t dealarr:browser .
#   (pages rendues en JavaScript uniquement ; jamais pour franchir une protection anti-bot)

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Paris \
    DEALARR_DB=/data/dealarr.db DEALARR_WATCHLIST=/config/watchlist.yaml \
    DEALARR_HOST=0.0.0.0 DEALARR_PORT=8080
WORKDIR /app
COPY pyproject.toml ./
COPY dealarr ./dealarr
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* \
    && pip install . && useradd --uid 10001 --create-home dealarr \
    && mkdir -p /data /config && chown dealarr /data
USER dealarr
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
ENTRYPOINT ["dealarr"]
CMD ["serve"]

FROM base AS browser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright DEALARR_BROWSER=true
RUN pip install ".[browser]" && playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright
USER dealarr

FROM base AS default
