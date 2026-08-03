#!/usr/bin/env bash
# FirstFrame — arranque de la demo, una sola orden.
#
#   bash demo/run_demo.sh              # limpia, arranca, siembra y deja la sala lista
#   bash demo/run_demo.sh --reset      # además tira TODOS los jobs viejos y siembra de cero
#   bash demo/run_demo.sh --no-seed    # solo levanta el servidor
#   bash demo/run_demo.sh --fg         # servidor en primer plano (Ctrl-C para parar)
#   bash demo/run_demo.sh --free       # imagen REAL en vez de mock (~47 s/escena): b-roll,
#                                      # NO para el plano del primer fotograma
#
# La configuración de abajo es la MEDIDA hoy, no la teórica:
#   DEMO_MODE=mock JUDGE_THRESHOLD=0 EVENTS_MODE=off HLS_SERVE_FROM=local
#   -> primer fotograma ~5,4 s.
#
# Por qué cada una:
#   DEMO_MODE=mock      generación con mocks/ffmpeg: coste $0 para los jueces y latencia
#                       reproducible. No hay proveedor de vídeo real disponible (PLAN §0).
#   JUDGE_THRESHOLD=0   apaga el juez de visión DURANTE LA GENERACIÓN. El juez de NIM en
#                       free tier tarda ~30 s por escena; con él, el primer fotograma se
#                       va a ~70 s y se arruina el número estrella.
#                       OJO: NO apaga el juez en el rechazo. `reject` entra por
#                       server/jobs.py:_refine_safe -> pipeline.runner:refine_scene(), que
#                       llama al juez SIEMPRE, sin mirar el threshold. Es decir: el juez se
#                       ve trabajando exactamente donde encaja en el guion (tramo 0:45–1:15)
#                       y no estorba donde no toca.
#   EVENTS_MODE=off     ni poller ni webhook tocan B2. La cuenta tiene el tope diario de
#                       transacciones agotado y cada listado es una Class C que falla.
#                       Los eventos de la UI siguen llegando: los emite el propio backend.
#   HLS_SERVE_FROM=local los segmentos se sirven del disco local, nunca de B2 (Class B).
#                       Es lo que hace que el player no se atragante con la cuota agotada.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
PORT="${PORT:-8000}"
URL="http://localhost:${PORT}"
LOG="${FIRSTFRAME_LOG:-/tmp/firstframe-demo.log}"

SEED=1
FG=0
GENMODE=mock
SEED_ARGS=()
for a in "$@"; do
  case "$a" in
    --no-seed) SEED=0 ;;
    --reset)   SEED_ARGS+=(--reset) ;;
    --fg)      FG=1 ;;
    --free)    GENMODE=free ;;   # imagen real (Pollinations), ~47 s/escena: SOLO para b-roll
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "opción desconocida: $a" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- comprobaciones
[ -x .venv/bin/uvicorn ] || { echo "falta .venv — crea el venv antes"; exit 1; }
command -v ffmpeg >/dev/null || { echo "falta ffmpeg en el PATH"; exit 1; }
[ -f .env ] || echo "AVISO: no hay .env — sin credenciales de B2 la app degrada a disco local"

# ---------------------------------------------------------------- entorno
set -a
[ -f .env ] && . ./.env
set +a

export DEMO_MODE=mock
# GEN_MODE gana sobre DEMO_MODE en pipeline/scenes.py. Se fija explicitamente para que un
# GEN_MODE suelto en .env o en el shell no meta el proveedor free (imagen real, ~47 s por
# escena medidos) en mitad de la demo y reviente el numero de primer fotograma.
export GEN_MODE="$GENMODE"
export JUDGE_THRESHOLD=0
export EVENTS_MODE=${EVENTS_MODE:-poll}   # poll: el feed del panel se puebla; off deja ese bloque muerto en camara
export HLS_SERVE_FROM=local
export MAX_ITERATIONS="${MAX_ITERATIONS:-1}"
export PORT

# ---------------------------------------------------------------- puerto limpio
# Sin pkill: pkill -f uvicorn se lleva por delante cualquier otro servidor del equipo.
PIDS=$(lsof -t -i ":${PORT}" -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  echo "[run] liberando el puerto ${PORT} (pids: $PIDS)"
  kill $PIDS 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    lsof -t -i ":${PORT}" -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.4
  done
  lsof -t -i ":${PORT}" -sTCP:LISTEN >/dev/null 2>&1 && kill -9 $PIDS 2>/dev/null || true
fi

# ---------------------------------------------------------------- limpieza de la sala
echo "[run] purgando jobs fallidos y de prueba…"
.venv/bin/python demo/seed.py --purge-only "${SEED_ARGS[@]+"${SEED_ARGS[@]}"}"

# ---------------------------------------------------------------- servidor
echo "[run] DEMO_MODE=$DEMO_MODE JUDGE_THRESHOLD=$JUDGE_THRESHOLD EVENTS_MODE=$EVENTS_MODE HLS_SERVE_FROM=$HLS_SERVE_FROM"

if [ "$FG" = 1 ]; then
  echo "[run] servidor en primer plano en ${URL} (Ctrl-C para parar)"
  exec .venv/bin/uvicorn server.app:app --port "$PORT" --log-level warning
fi

echo "[run] arrancando uvicorn — log en ${LOG}"
nohup .venv/bin/uvicorn server.app:app --port "$PORT" --log-level warning >"$LOG" 2>&1 &
SRV=$!
trap 'true' EXIT

for _ in $(seq 1 60); do
  curl -sf "${URL}/api/health" >/dev/null 2>&1 && break
  kill -0 "$SRV" 2>/dev/null || { echo "[run] el servidor murió al arrancar:"; tail -20 "$LOG"; exit 1; }
  sleep 0.5
done
curl -sf "${URL}/api/health" >/dev/null || { echo "[run] health no responde:"; tail -20 "$LOG"; exit 1; }
echo "[run] servidor arriba (pid $SRV)"

# ---------------------------------------------------------------- datos precargados
if [ "$SEED" = 1 ]; then
  .venv/bin/python demo/seed.py --seed-only --url "$URL" "${SEED_ARGS[@]+"${SEED_ARGS[@]}"}"
fi

# ---------------------------------------------------------------- resumen
echo
HEALTH=$(curl -s "${URL}/api/health")
.venv/bin/python -c '
import json, sys
h = json.loads(sys.argv[1])
print("[run] modo={mode} eventos={events_mode} b2={b2} jobs={jobs}".format(**h))
print("[run] hls desde: " + str(h.get("hls_served_from")))
if h.get("degraded"):
    print("[run] AVISO: " + str(h.get("warning")))
    print("[run] -> el badge de Object Lock no saldra hasta que suba la cuota de B2.")
' "$HEALTH"
echo
echo "[run] ABRE  ${URL}"
echo "[run] guion ${ROOT}/demo/GUION.md"
echo "[run] parar: kill $SRV     log: tail -f ${LOG}"
