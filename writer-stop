#!/usr/bin/env bash
SYSTEM_DIR="$(dirname "$(readlink -f "$0")")"
echo "Stopping Writer System..."
cd "$SYSTEM_DIR"
docker compose down
echo "Writer System stopped."
