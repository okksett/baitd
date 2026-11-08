#!/usr/bin/env sh
# Baitd launcher (macOS / Linux): creates .venv, installs, starts the app.
set -e
cd "$(dirname "$0")"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Falta Python 3.10 o superior (o la version instalada es mas antigua)."
  echo "Instalalo desde https://www.python.org/downloads/ y vuelve a ejecutar ./iniciar.sh"
  exit 1
fi

case "$(pwd)" in
  *OneDrive*) echo "AVISO: esta carpeta esta dentro de OneDrive. Muevela a una ruta corta y sin sincronizar (por ejemplo ~/baitd) para evitar errores al instalar." ;;
esac

if [ ! -x .venv/bin/python ]; then
  echo "Creando el entorno (.venv)..."
  "$PY" -m venv .venv
fi
# Install only for a new .venv or a changed pyproject.toml, so an installed app also starts offline.
STAMP=.venv/.instalado
if [ ! -f "$STAMP" ] || [ pyproject.toml -nt "$STAMP" ]; then
  echo "Instalando dependencias (la primera vez tarda unos minutos)..."
  .venv/bin/python -m pip install --quiet -e .
  touch "$STAMP"
fi
exec .venv/bin/python -m baitd "$@"
