#!/usr/bin/env bash
# Equivalente Linux/Ubuntu de ejecutar_dev.bat: levanta el servidor de
# desarrollo (con --reload) en el puerto 8001.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d "venv" ]; then
    echo "No se encontró ./venv. Creándolo..."
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
fi

source venv/bin/activate

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

exec uvicorn app:app --host 0.0.0.0 --port 8001 --reload
