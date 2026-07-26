#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
SERVICE_TEMPLATE="$PROJECT_DIR/config/local-vision.service.example"
SERVICE_NAME=local-vision-console.service
SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
LOCAL_VISION_USER=${LOCAL_VISION_USER:-$(id -un)}
LOCAL_VISION_HOME=$(getent passwd "$LOCAL_VISION_USER" | cut -d: -f6)
TEMP_UNIT=$(mktemp)

cleanup() {
    rm -f "$TEMP_UNIT"
}
trap cleanup EXIT HUP INT TERM

install -d -m 0700 "$LOCAL_VISION_HOME/.config/local-vision-console"
sed \
    -e "s|LOCAL_VISION_USER|$LOCAL_VISION_USER|g" \
    -e "s|LOCAL_VISION_PROJECT_DIR|$PROJECT_DIR|g" \
    -e "s|LOCAL_VISION_HOME|$LOCAL_VISION_HOME|g" \
    "$SERVICE_TEMPLATE" > "$TEMP_UNIT"

sudo -S install -m 0644 "$TEMP_UNIT" "$SERVICE_PATH"
sudo -S systemctl daemon-reload
sudo -S systemctl enable --now "$SERVICE_NAME"

printf '%s\n' "Local Vision installed: http://$(hostname -I | awk '{print $1}'):7127/"
