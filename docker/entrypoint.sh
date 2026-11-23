#!/bin/sh
# Container start-up: make sure the database exists (collections, validators, indexes, seed data: idempotent, never deletes
# data), then run the command (by default the API server, which also serves the dashboard).
set -e

# With WITH_SPARK=1 the image has Java 21; Spark finds it through JAVA_HOME.
if [ -z "$JAVA_HOME" ] && command -v java >/dev/null 2>&1; then
    JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
    export JAVA_HOME
fi

if [ "${SKIP_DB_INIT:-0}" != "1" ]; then
    python scripts/init_db.py
fi

exec "$@"
