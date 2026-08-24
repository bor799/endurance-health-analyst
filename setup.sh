#!/usr/bin/env bash
# Bootstrap the endurance-health-analyst environment.
# Creates .venv and installs DuckDB plus the local image pipeline.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [ -x "$ROOT/.venv/bin/python" ] && "$ROOT/.venv/bin/python" -c "import duckdb, PIL, pillow_heif" 2>/dev/null; then
  echo "OK: .venv already ready ($($ROOT/.venv/bin/python -c 'import duckdb; print(duckdb.__version__)'))"
  exit 0
fi

if command -v uv >/dev/null 2>&1; then
  uv venv --system-site-packages --allow-existing "$ROOT/.venv"
  uv pip install --python "$ROOT/.venv/bin/python" -r "$ROOT/requirements.txt"
else
  python3 -m venv --system-site-packages "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install --upgrade pip
  "$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"
fi

"$ROOT/.venv/bin/python" -c "import duckdb, sys; print('duckdb', duckdb.__version__, 'on', sys.version.split()[0])"
echo "Done. Run scripts with: $ROOT/.venv/bin/python $ROOT/scripts/<script>.py"
