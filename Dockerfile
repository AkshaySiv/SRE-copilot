# ---- UI build ----------------------------------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Python deps -------------------------------------------------------------------------------
FROM python:3.12-slim AS deps
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY backend/pyproject.toml ./
RUN python -c "import tomllib;print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > requirements.txt \
 && pip install --prefix=/install -r requirements.txt

# ---- Runtime -----------------------------------------------------------------------------------
FROM python:3.12-slim
LABEL org.opencontainers.image.title="sre-copilot" \
      org.opencontainers.image.description="AI SRE copilot - incident investigation and approval-gated remediation"
RUN groupadd -g 10001 app && useradd -u 10001 -g app -m -s /usr/sbin/nologin app \
 && apt-get update && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=deps /install /usr/local
WORKDIR /app
COPY backend/app ./app
COPY --from=ui /ui/dist ./static
RUN mkdir -p /data && chown app:app /data
USER 10001
ENV SRECOPILOT_STATIC_DIR=/app/static \
    SRECOPILOT_DATA_DIR=/data \
    SRECOPILOT_DATABASE_URL=sqlite+aiosqlite:////data/sre-copilot.db \
    SRECOPILOT_LOG_JSON=true \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
