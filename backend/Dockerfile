# Falcon FastAPI backend — production image.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (falcon package, app package, config.yaml, tests harness).
COPY falcon ./falcon
COPY app ./app
COPY config.yaml ./config.yaml
COPY tests ./tests

# Run as a non-root user.
RUN useradd --create-home --uid 10001 falcon \
    && chown -R falcon:falcon /app
USER falcon

EXPOSE 8000

# App Platform / most PaaS inject $PORT; default to 8000 locally.
# One worker: background daemon threads (audit, extraction, dual-run) and the
# in-process Mongo client are per-process; scale out with more instances, not
# more workers, so those threads aren't duplicated per request cycle.
CMD ["sh", "-c", "echo '=== ENV DEBUG ===' && echo OPENROUTER_SET=$(if [ -n \"$OPENROUTER_API_KEY\" ]; then echo yes; else echo NO; fi) && echo MONGODB_SET=$(if [ -n \"$MONGODB_URI\" ]; then echo yes; else echo NO; fi) && echo PORT=$PORT && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
