# ---------- 阶段 1：构建前端 ----------
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- 阶段 2：Python 运行时 ----------
FROM python:3.12-slim
WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    fastapi 'uvicorn[standard]' sqlalchemy pydantic pydantic-settings PyJWT bcrypt \
    pandas numpy apscheduler httpx pyarrow python-multipart yfinance \
    && (pip install --no-cache-dir --no-deps jsonpath || true) \
    && (pip install --no-cache-dir akshare || echo "akshare 安装失败，A股数据源不可用") \
    && (pip install --no-cache-dir futu-api || echo "futu-api 安装失败，富途不可用") \
    && (pip install --no-cache-dir ib_async || echo "ib_async 安装失败，IBKR 不可用")

COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
