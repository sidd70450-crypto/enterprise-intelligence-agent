# Build stage: resolve and install locked dependencies into a virtualenv.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend ./backend
RUN uv sync --frozen --no-dev

# Runtime stage: no build tools, non-root user.
FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home eia \
 && mkdir -p /var/lib/eia/uploads /var/lib/eia/models \
 && chown -R eia /var/lib/eia
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/backend /app/backend
COPY alembic.ini /app/alembic.ini
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    EIA_STORAGE_DIR=/var/lib/eia/uploads EIA_EMBEDDING_CACHE_DIR=/var/lib/eia/models
WORKDIR /app
USER eia
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
CMD ["uvicorn", "backend.main:app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
