FROM python:3.12-slim

# uv нужен и в образе: зависимости ставятся строго из uv.lock, тем же
# резолвом, что и локально.
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/.venv \
    PATH=/srv/.venv/bin:$PATH

WORKDIR /srv

# Зависимости отдельным слоем: правка кода не тянет за собой переустановку.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app/ ./app/
COPY kb/ ./kb/

# Сервис не делает ничего, что требует root. Каталог состояния монтируется
# томом, поэтому на хосте он должен принадлежать тому же uid:
#   mkdir -p data && chown 1000:1000 data
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown app:app /data /srv
USER app

# Живость определяем по отметке в конце каждого цикла: зависший HTTP-вызов
# внутри цикла логов не оставит, а heartbeat перестанет обновляться.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-m", "app.healthcheck"]

CMD ["python", "-m", "app.main"]
