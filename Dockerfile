# Pin PYTHON_IMAGE to the deployment team's approved digest before release.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE} AS core
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp DEEPEVAL_TELEMETRY_OPT_OUT=YES OTEL_SDK_DISABLED=true \
    RAGAS_DO_NOT_TRACK=true DEEPEVAL_FILE_SYSTEM=READ_ONLY
WORKDIR /app
RUN python -m venv /opt/venvs/core \
    && ln -s /opt/venvs/core /app/.venv \
    && mkdir -p /app/reports \
    && chown 10001:10001 /app/reports
COPY qa/ ./qa/
COPY scripts/ ./scripts/
COPY suites/ ./suites/
COPY datasets/ ./datasets/
COPY sources/ ./sources/
COPY config/settings.json config/payload.template.json ./config/
COPY requirements*.txt run.py ./*.command ./
COPY deploy/aks/ ./deploy/aks/
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/python", "/app/deploy/aks/entrypoint.py"]
CMD ["--operation", "core", "--mode", "plan", "--run-id", "aks-plan-001", "--case-ids", "BASELINE-001"]

# Full campaign/safety runs require the separate, pinned native environments.
FROM core AS full
USER root
RUN python -m venv /opt/venvs/ragas \
    && python -m venv /opt/venvs/deepeval \
    && /opt/venvs/ragas/bin/python -m pip install --no-cache-dir -r requirements-campaign-ragas.txt \
    && /opt/venvs/deepeval/bin/python -m pip install --no-cache-dir -r requirements-deepeval.txt \
    && /opt/venvs/ragas/bin/python -m pip check \
    && /opt/venvs/deepeval/bin/python -m pip check \
    && ln -s /opt/venvs/ragas /app/.venv-ragas \
    && ln -s /opt/venvs/deepeval /app/.venv-deepeval
USER 10001:10001
