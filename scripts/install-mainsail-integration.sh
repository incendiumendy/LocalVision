#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
NAVIGATION=${LOCAL_VISION_MAINSAIL_NAVIGATION:-"$HOME/printer_data/config/.theme/navi.json"}
NAVIGATION_ENTRY="$PROJECT_DIR/config/mainsail-navigation.json"
NGINX_SITE=${LOCAL_VISION_NGINX_SITE:-"/etc/nginx/sites-available/mainsail"}
NGINX_LOCATION="$PROJECT_DIR/config/nginx-location.conf"
NGINX_SNIPPET="/etc/nginx/snippets/local-vision.conf"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="$HOME/printer_data/config/.local-vision-backup/$STAMP-mainsail"
TEMP_NAVIGATION=$(mktemp)
TEMP_NGINX=$(mktemp)

cleanup() {
    rm -f "$TEMP_NAVIGATION" "$TEMP_NGINX"
}
trap cleanup EXIT HUP INT TERM

python3 "$SCRIPT_DIR/mainsail_integration.py" navigation \
    "$NAVIGATION" "$NAVIGATION_ENTRY" "$TEMP_NAVIGATION"
python3 -m json.tool "$TEMP_NAVIGATION" >/dev/null
python3 "$SCRIPT_DIR/mainsail_integration.py" nginx \
    "$NGINX_SITE" "$TEMP_NGINX"

mkdir -p "$BACKUP_DIR"
cp -p "$NAVIGATION" "$BACKUP_DIR/navi.json"
sudo -S cp -p "$NGINX_SITE" "$BACKUP_DIR/nginx-mainsail"
cp "$TEMP_NAVIGATION" "$NAVIGATION"
sudo -S install -m 0644 "$NGINX_LOCATION" "$NGINX_SNIPPET"
sudo -S install -m 0644 "$TEMP_NGINX" "$NGINX_SITE"

if ! sudo -S nginx -t; then
    cp "$BACKUP_DIR/navi.json" "$NAVIGATION"
    sudo -S cp "$BACKUP_DIR/nginx-mainsail" "$NGINX_SITE"
    sudo -S nginx -t
    printf '%s\n' "Nginx validation failed; originals restored." >&2
    exit 1
fi

if ! sudo -S systemctl reload nginx; then
    cp "$BACKUP_DIR/navi.json" "$NAVIGATION"
    sudo -S cp "$BACKUP_DIR/nginx-mainsail" "$NGINX_SITE"
    sudo -S nginx -t
    sudo -S systemctl reload nginx
    printf '%s\n' "Nginx reload failed; originals restored." >&2
    exit 1
fi

printf '%s\n' "Local Vision added to Mainsail: /local-vision/"
printf '%s\n' "Backup created: $BACKUP_DIR"
