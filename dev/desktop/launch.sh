#!/usr/bin/env bash
set -euo pipefail
mkdir -p "$PIXELQSO_DATA_DIR"
if [[ ! -e "$PIXELQSO_DATA_DIR/settings.ini" ]]; then
    printf '[radio]\nauto_connect_cat=false\n' > "$PIXELQSO_DATA_DIR/settings.ini"
fi
exec /opt/app-venv/bin/python -u -m pixelqso
