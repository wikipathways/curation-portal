# curation-portal — production image (issue #5).
# Built by CI and pushed to GHCR so both swarm nodes can pull (real failover); see
# docs/deployment.md. Runs from the copied source tree (not the installed wheel) so the
# templates/, static/, and migrations/ dirs resolve relative to /app.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first (better layer caching). Installing the project pulls its deps + the
# PostgreSQL driver; the app itself is run from the source copied below via `python -m`.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install ".[postgres]"

COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY templates ./templates
COPY static ./static
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd --create-home appuser \
    && chown -R appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
# `python -m uvicorn` puts /app (cwd) on sys.path[0], so the source `app` package + its sibling
# templates/static win over the installed copy.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
