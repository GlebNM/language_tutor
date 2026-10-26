FROM node:24.10.0-bookworm-slim AS web
WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY apps/web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.8.6 AS uv
FROM python:3.12.11-slim-bookworm AS runtime
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-cache
COPY backend/ ./backend/
COPY data/demo/ ./data/demo/
COPY data/diagnostics/ ./data/diagnostics/
COPY --from=web /web/dist/ ./apps/web/dist/
RUN groupadd --gid 10001 tutor && useradd --uid 10001 --gid tutor --no-create-home tutor && mkdir -p /data && chown tutor:tutor /data
USER tutor
EXPOSE 8765
CMD [".venv/bin/uvicorn", "backend.app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]
