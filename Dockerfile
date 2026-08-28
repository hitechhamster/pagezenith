# 2026-08：不再需要 Playwright 官方镜像 —— 抓取走 httpx / Exa contents，
# 服务器内存有限（与观象台共存），不装 Chromium。
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY api ./api
COPY web ./web
COPY scripts ./scripts

# 卡密库落在挂载卷里（容器重建不丢）
ENV BILLING_DB=/data/billing.db \
    USE_MOCKS=false \
    FETCH_MODE=httpx \
    SERP_PROVIDER=serper \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]

WORKDIR /app/api
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
