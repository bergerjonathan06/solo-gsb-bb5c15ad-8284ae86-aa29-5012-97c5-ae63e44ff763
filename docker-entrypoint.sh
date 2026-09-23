#!/usr/bin/env bash
set -euo pipefail

# Initialise the SQLite database (creates the data directory, tables and
# indexes) before serving traffic.
python -m app.init_db

exec "$@"
