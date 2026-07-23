#!/usr/bin/env bash
# Production startup script for Quotex Flask Signal Dashboard
set -euo pipefail

# Ensure the Python virtualenv/libs are always on PATH regardless of environment
export PATH="/home/runner/workspace/.pythonlibs/bin:$PATH"

# Navigate to the webapp directory (app.py bootstraps its own sys.path)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}/Quotex/market_analyzer/webapp"

PORT="${PORT:-5000}"
export PORT

# ── Pre-flight health check ──────────────────────────────────────────────
# Verifies Python env, dependency imports, Playwright/Chromium, config, and
# required files exist before we bind to a port. Aborts startup only on
# CRITICAL failures (e.g. a required package won't import). Missing Quotex
# session / Chromium are reported as warnings, not startup blockers, since
# the dashboard can still serve and login can happen afterward.
echo "Running pre-flight health check..."
if ! /home/runner/workspace/.pythonlibs/bin/python3 preflight_check.py; then
    echo "Pre-flight health check FAILED — aborting startup." >&2
    exit 1
fi

echo "Starting Quotex Signal Dashboard on port $PORT..."

exec /home/runner/workspace/.pythonlibs/bin/gunicorn -c gunicorn.conf.py app:app
