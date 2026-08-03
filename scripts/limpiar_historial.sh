#!/usr/bin/env bash
# Saca el reconocimiento de la competencia del HISTORIAL, no solo del arbol.
#
# Por que: esos ficheros analizan ~150 submissions rivales con nombre y emiten
# juicios sobre ellas ("baja senal", "esqueleto vacio"). Un `git rm` las quita del
# arbol pero cualquiera las recupera con `git log`. Si el repo se hace publico —o
# se invita a b2genblaze, que son los jueces— eso se lee, y sienta mal.
#
# Lo que SI se queda: `research/genblaze-sdk.md` y `research/sdk-feedback.md`.
# Son trabajo tecnico sobre el SDK del sponsor y sustentan los PRs que mandamos.
#
# Verificado antes de escribir esto: el historial NO contiene secretos (.env
# nunca se commiteo), asi que esto es solo cuestion de presentacion.
#
# EJECUTAR SOLO CUANDO NINGUN AGENTE ESTE COMMITEANDO: reescribe 120 commits y
# obliga a un force-push. Si alguien empuja a la vez, se pierde su trabajo.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v git-filter-repo >/dev/null 2>&1 || export PATH="$HOME/.local/bin:$PATH"

echo "== 1. arbol limpio? =="
if [ -n "$(git status --porcelain)" ]; then
  echo "   HAY CAMBIOS SIN COMMITEAR. Commitea o guarda antes de reescribir."
  git status -s | head
  exit 1
fi

echo "== 2. copia de seguridad =="
BK="../genblaze-hackathon-backup-$(date +%H%M%S)"
cp -a . "$BK"
echo "   copia en $BK  (si algo sale mal, esta entera aqui)"

echo "== 3. reescribiendo =="
git filter-repo --force \
  --path research/recon-a.md \
  --path research/recon-b.md \
  --path research/SINTESIS.md \
  --path research/lateral.md \
  --path research/ideas-b2b.md \
  --path research/ideas-consumer.md \
  --path research/ideas-infra.md \
  --path research/ideas-wildcard.md \
  --invert-paths

echo "== 4. comprobacion =="
if git log --all --oneline -- research/recon-a.md 2>/dev/null | grep -q .; then
  echo "   FALLO: siguen en el historial"; exit 1
fi
echo "   el recon ya no esta en el historial"
git log --all --format= --name-only 2>/dev/null | grep -c '^research/' | xargs echo "   ficheros de research que quedan (referencias):"
ls research/ 2>/dev/null | sed 's/^/   /'

echo "== 5. remoto =="
git remote -v | head -1 | grep -q origin || git remote add origin git@github.com:migarci2/genblaze-hackathon.git
echo "   filter-repo borra el remoto a proposito; reanadido si hacia falta."
echo
echo "AHORA, y solo si todo lo de arriba esta bien:"
echo "   git push --force origin master"
