#!/bin/sh
set -eu

echo "[db-migrate] waiting for story database"
python docker/scripts/wait_for_db.py

echo "[db-migrate] applying migrations"
python -m story_db.apply_migrations
