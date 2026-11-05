# =========================================================
# Builder
# Installs Python dependencies separately so the final
# runtime image can contain only the required artifacts.
# =========================================================

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /build

COPY requirements.txt requirements-core.txt ./

RUN pip install \
    --no-cache-dir \
    --only-binary=:all: \
    --prefix=/install \
    -r requirements.txt


# =========================================================
# Runtime
# API-only runtime containing application and client dependencies.
# =========================================================

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ENV RAG_ENABLED=false

WORKDIR /app

# Copy installed Python packages from builder.
COPY --from=builder /install /usr/local

# Copy application source code.
COPY --chown=10001:10001 app ./app

# Create non-root runtime user.
RUN groupadd \
    --gid 10001 \
    appuser \
    && useradd \
    --uid 10001 \
    --gid 10001 \
    --create-home \
    appuser

USER 10001

EXPOSE 8080

# Start FastAPI application.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--limit-concurrency", "32", "--backlog", "64", "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "30"]