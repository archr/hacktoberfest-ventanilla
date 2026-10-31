#!/usr/bin/env bash
# Levanta el servicio de documentos simulado (:8701) y el chat (:8700).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
APP_PORT=${APP_PORT:-8700}
MOCK_PORT=${MOCK_PORT:-8701}
HOST=${HOST:-127.0.0.1}

if [ ! -f data/index.sqlite ]; then
  echo "⚠  No hay índice. Ejecuta primero: $PY -m app.rag.ingest" >&2
fi

$PY -m uvicorn mock_doc_service.main:app --host 127.0.0.1 --port "$MOCK_PORT" --log-level warning &
MOCK_PID=$!
trap 'kill $MOCK_PID 2>/dev/null || true' EXIT

echo "Chat:  http://$HOST:$APP_PORT"
echo "Presentación: http://$HOST:$APP_PORT/presentacion/"
echo "Mock de documentos: http://127.0.0.1:$MOCK_PORT/docs"
exec $PY -m uvicorn app.main:app --host "$HOST" --port "$APP_PORT"
