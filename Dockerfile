FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend/dev \
    TZ=Asia/Shanghai
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt \
    && groupadd --gid 10001 trading \
    && useradd --uid 10001 --gid trading --create-home trading
COPY backend/main.py /app/backend/main.py
COPY backend/dev/market_trading_bot/ /app/backend/dev/market_trading_bot/
COPY --from=frontend /build/frontend/dist/ /app/frontend/dist/
RUN mkdir -p /app/backend/dev/data /app/backend/dev/logs \
    && chown -R trading:trading /app/backend/dev/data /app/backend/dev/logs
USER trading
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/api/strategies', timeout=3)"
CMD ["python", "backend/main.py", "--host", "0.0.0.0", "--port", "8081"]
