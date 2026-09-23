# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    COPILOT_CLI_EXTRACT_DIR=/opt/copilot-runtime

WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && python -m copilot download-runtime \
    && chmod -R a+rX /opt/copilot-runtime

COPY . .
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin odin \
    && mkdir /data && chown odin:odin /data

# /data : base SQLite, jeton Copilot enregistre et dossiers temporaires du SDK (cwd).
ENV COPILOT_SKIP_CLI_DOWNLOAD=1 \
    HOME=/data \
    ODIN_DB_PATH=/data/oracleiq.db \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080
USER odin
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8080
# SIGINT declenche l'arret propre des trois services de "oracleiq.py all".
STOPSIGNAL SIGINT
CMD ["python", "/app/oracleiq.py", "all"]
