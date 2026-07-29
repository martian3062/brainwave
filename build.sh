#!/usr/bin/env bash
# Render build. Run BY RENDER, not by this repo's tooling and not by any agent.
#
# Invoked as `bash build.sh` from render.yaml rather than `./build.sh`: the
# executable bit does not survive a clone from a Windows checkout, and the
# failure mode is a deploy that dies with "Permission denied" before a single
# line of this file runs.
set -o errexit
set -o pipefail
set -o nounset

python -m pip install --upgrade pip
pip install -r requirements.txt

# ---------------------------------------------------------------------------
# Schema. Alembic owns it in every real deployment -- `app.db.create_all()` only
# ever runs against the local SQLite fallback, so this line is the one thing
# standing between a deploy and an empty database.
#
# alembic/env.py takes the URL from app.db (which rewrites Render's legacy
# `postgres://` scheme that SQLAlchemy 2 rejects), which is why alembic.ini
# deliberately carries no URL.
# ---------------------------------------------------------------------------
alembic upgrade head
alembic current

# ---------------------------------------------------------------------------
# Conformance gate. `doctor --skip-ledger` runs the protocol checks only: no
# database, no network, about a second. It catches the class of failure that is
# otherwise invisible until an agent's first paid call -- a malformed `accepts`,
# a non-CAIP-2 network, an `amount` that is not an atomic-unit string, a drifted
# MCP `_meta` key after an x402 bump.
#
# A build that cannot produce a valid 402 should not become a running payment
# gateway. Set SKIP_DOCTOR=1 in the Render dashboard to bypass it in an
# emergency.
# ---------------------------------------------------------------------------
if [ "${SKIP_DOCTOR:-0}" != "1" ]; then
  python -m app.cli doctor --skip-ledger --no-color
fi

echo "build ok -- start with: uvicorn app.main:app --host 0.0.0.0 --port \$PORT"
