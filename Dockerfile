# Production image for Cloud Run. Build: gcloud builds submit (see deploy/gcp/README.md)

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml ./
COPY app ./app
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENVIRONMENT=production \
    PORT=8080
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin cdn
WORKDIR /srv
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links=/wheels cdnmanager && rm -rf /wheels
COPY alembic.ini ./
COPY alembic ./alembic
USER 10001
EXPOSE 8080
# Cloud Run sends SIGTERM and allows 10 s before SIGKILL.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --no-proxy-headers --no-access-log --timeout-graceful-shutdown 8
