#!/usr/bin/env bash
# URL publica sin cuenta ni registro, como plan B del deploy en Fly.
#
# Probado: HTTP 200 en ~1 s desde fuera. Dos avisos antes de usarlo:
#   - la URL cambia en cada arranque, asi que para la submission de Devpost es
#     peor que Fly (fly.toml ya esta listo y es un solo comando);
#   - expone la app a internet SIN autenticacion mientras corre. Cualquiera con
#     la URL puede crear jobs y gastar cuota de B2. Levantalo solo cuando lo
#     necesites y cierralo con Ctrl-C al terminar.
#
#   bash demo/tunel.sh
set -euo pipefail
cd "$(dirname "$0")/.."

CF="$HOME/.local/bin/cloudflared"
[ -x "$CF" ] || { echo "falta cloudflared en $CF"; exit 1; }
curl -sf -o /dev/null http://localhost:8000/api/health \
  || { echo "el servidor no responde en :8000 — arranca antes 'bash demo/run_demo.sh'"; exit 1; }

: > /tmp/tunnel.log
"$CF" tunnel --url http://localhost:8000 >>/tmp/tunnel.log 2>&1 &
TUNNEL_PID=$!
trap 'kill "$TUNNEL_PID" 2>/dev/null || true; echo; echo "tunel cerrado"' EXIT INT TERM

for _ in $(seq 1 30); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/tunnel.log | head -1 || true)
  [ -n "${URL:-}" ] && break
  sleep 1
done
[ -n "${URL:-}" ] || { echo "no se pudo obtener la URL, mira /tmp/tunnel.log"; exit 1; }

echo "URL publica: $URL"
echo "Ctrl-C para cerrarla."
wait "$TUNNEL_PID"
