# FirstFrame - imagen de despliegue.
#
# python:3.13-slim + ffmpeg. Nada de serverless: el assembler remuxa fMP4 y sube
# segmentos mientras el pipeline sigue generando, asi que necesitamos un PROCESO
# PERSISTENTE con background tasks vivas entre peticiones y streams SSE largos.
# Un unico worker de uvicorn a proposito: el estado del assembler y la cola de
# eventos viven en memoria del proceso.
#
#   docker build -t firstframe .
#   docker run --rm -p 8000:8000 --env-file .env firstframe

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: lo exige FFmpegCompositor (fan-in de audio+video) y el assembler HLS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# requirements primero: capa cacheada, el codigo cambia mucho mas que las deps.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data es el unico directorio escribible: sqlite, work dir del assembler,
# playlists HLS y flags de chaos. Sirve tal cual con o sin volumen montado.
ENV DEMO_MODE=mock \
    EVENTS_MODE=poll \
    PORT=8000 \
    FIRSTFRAME_DB=/data/firstframe.db \
    FIRSTFRAME_WORK=/data/work \
    FIRSTFRAME_HLS=/data/hls \
    CHAOS_FILE=/data/chaos.json

RUN useradd --create-home --uid 10001 firstframe \
 && mkdir -p /data \
 && chown -R firstframe:firstframe /data /app
USER firstframe

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/api/health" || exit 1

CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 75"]
