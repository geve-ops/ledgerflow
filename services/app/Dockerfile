# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN python -m venv /opt/venv && /opt/venv/bin/pip install .

FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /opt/venv /opt/venv
# Non-root, fixed UID/GID so the pod can satisfy the "restricted" Pod Security Standard.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
USER 10001:10001
EXPOSE 8000 9100
# Default = API. The worker Deployment overrides the command:
#   python -m ledgerflow.worker.main
CMD ["uvicorn", "ledgerflow.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
