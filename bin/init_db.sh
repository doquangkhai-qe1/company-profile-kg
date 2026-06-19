#!/usr/bin/env bash
# Ensure Neo4j constraints/indexes for the Tier-1 schema. Idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PY="${PYTHON:-$HERE/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

PYTHONPATH="$HERE/src" "$PY" - <<'PYEOF'
from cpkg.config import load_config
from cpkg import kgtier1

cfg = load_config()
if cfg.missing:
    raise SystemExit(f"missing env: {cfg.missing} (env file: {cfg.env_file})")
driver = kgtier1.connect(cfg)
try:
    n = kgtier1.ensure_schema(driver, cfg.neo4j_database)
    print(f"ok: applied {n} DDL statements to {cfg.neo4j_uri}/{cfg.neo4j_database}")
finally:
    driver.close()
PYEOF
