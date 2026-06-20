#!/usr/bin/env bash
# Launch the data-review UI: inspect every pipeline layer (crawl/extract/tier1/tier2)
# per ticker & run, in the browser. Read-only; Neo4j layers degrade-safe if the DB is down.
#
#   bash bin/review.sh            # http://127.0.0.1:8765
#   bash bin/review.sh --port 9000 --host 0.0.0.0
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"
exec env PYTHONPATH=src "$PY" -m cpkg.review_app "$@"
