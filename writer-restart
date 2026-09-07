#!/usr/bin/env bash
SYSTEM_DIR="$(dirname "$(readlink -f "$0")")"
echo "Restarting Writer System..."
"$SYSTEM_DIR/writer-stop"
"$SYSTEM_DIR/writer-start"
