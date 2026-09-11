# LexVault single-process container. The model service stays an explicit
# external dependency: nothing downloads weights at startup.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/lexvault-venv \
    LAW_REVIEW_DATA_DIR=/data \
    LAW_REVIEW_JSON_LOGS=1

# Native document parsers: pdftotext/pdfinfo/pdftoppm (poppler) and Chinese
# OCR (tesseract + chi_sim). No other system packages on purpose.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY app ./app
COPY scripts ./scripts
COPY benchmarks/lawbench ./benchmarks/lawbench
RUN uv sync --locked --no-dev

# Non-root runtime identity; /data holds the SQLite database, uploads,
# exports and checkpoints.
RUN useradd --system --uid 10001 --home-dir /nonexistent lexvault \
    && mkdir -p /data \
    && chown lexvault:lexvault /data
USER lexvault

EXPOSE 8000
# One API process per data directory is a documented ownership requirement;
# scale workers, not replicas of this container against shared data.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8000/api/health', timeout=4).read()"]

CMD ["/opt/lexvault-venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
