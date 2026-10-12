# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS build
WORKDIR /src
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml ./
COPY app ./app
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    USER=app \
    HOME=/home/app \
    FORWARDED_ALLOW_IPS="*"
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
WORKDIR /srv
COPY alembic.ini ./
COPY alembic ./alembic
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
# One process on purpose: live sessions are held in memory (docs/limitations-and-risks.md, A1).
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--ws-max-size", "16777216", "--timeout-graceful-shutdown", "20", "--no-access-log"]
