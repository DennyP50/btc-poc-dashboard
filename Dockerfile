FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/poc.sqlite3 \
    AGGTRADES_CACHE=/data/aggtrades \
    PORT=8050

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile numpy==2.1.3 pandas==2.2.3 && \
    pip install --no-cache-dir --no-compile -r requirements.txt && \
    useradd --system --uid 10001 --create-home appuser && \
    mkdir -p /data/aggtrades && chown -R appuser:appuser /data

COPY --chown=appuser:appuser app.py poc_engine.py poc_store.py strategy_engine.py exact_poc.py strategy_job.py daily_exact_job.py context_data_job.py ./
COPY --chown=appuser:appuser assets ./assets

USER appuser
EXPOSE 8050

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8050')+'/health', timeout=3)" || exit 1

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8050} --workers 1 --threads 4 --timeout 120 --access-logfile - app:server"]
