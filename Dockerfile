# Playwright 官方镜像：自带 Chromium + 系统依赖（playwright 版本须与 requirements 对齐）
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt
# 确保浏览器二进制就位（基础镜像已含，版本一致时近乎 no-op）
RUN python -m playwright install chromium

COPY api ./api
COPY web ./web

# 容器内无系统 Chrome → 用 Playwright 自带 Chromium（BROWSER_CHANNEL 置空）
ENV BROWSER_CHANNEL="" \
    USE_MOCKS=false \
    PYTHONUNBUFFERED=1

WORKDIR /app/api
# Render 通过 $PORT 注入端口
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
