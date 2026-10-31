#!/bin/sh
set -eu
if [ "${AUTO_INGEST:-false}" = true ]; then
  python - <<'PY'
import subprocess
import sys
from app.rag.store import IndexStore
from app.settings import get_settings

store = IndexStore(get_settings().index_path)
empty = store.stats()["chunks"] == 0
store.close()
if empty:
    subprocess.run([sys.executable, "-m", "app.rag.ingest"], check=True)
PY
fi
exec "$@"
