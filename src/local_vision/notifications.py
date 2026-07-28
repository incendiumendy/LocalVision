"""Private-network notification helpers for Local Vision."""

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse


class NotificationConfigurationError(ValueError):
    """Raised when a notification destination is unsafe or incomplete."""


def _is_local_address(address):
    ip = ipaddress.ip_address(address)
    return ip.is_private or ip.is_loopback or ip.is_link_local


def validate_home_assistant_webhook_url(value):
    """Validate a private Home Assistant webhook URL without exposing it."""
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise NotificationConfigurationError(
            "Die Home-Assistant-Webhook-URL muss mit http:// oder https:// "
            "beginnen.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise NotificationConfigurationError(
            "Die Home-Assistant-Webhook-URL ist ungültig.")
    if parsed.query or parsed.fragment:
        raise NotificationConfigurationError(
            "Query und Fragment gehören nicht in die Webhook-URL.")
    if not parsed.path.startswith("/api/webhook/") or not parsed.path.removeprefix(
            "/api/webhook/").strip("/"):
        raise NotificationConfigurationError(
            "Erwartet wird eine Home-Assistant-URL mit /api/webhook/<ID>.")
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(parsed.hostname, parsed.port)
        }
    except socket.gaierror as exc:
        raise NotificationConfigurationError(
            "Der Home-Assistant-Hostname ist nicht auflösbar.") from exc
    if not addresses or not all(_is_local_address(item) for item in addresses):
        raise NotificationConfigurationError(
            "Home Assistant muss über eine lokale/private Adresse erreichbar "
            "sein.")
    return value


def send_home_assistant_webhook(webhook_url, event, timeout_seconds=10):
    """POST one JSON event to a validated Home Assistant webhook."""
    url = validate_home_assistant_webhook_url(webhook_url)
    if not url:
        raise NotificationConfigurationError(
            "Kein Home-Assistant-Webhook konfiguriert.")
    body = json.dumps(
        event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "LocalVision/0.1",
        })
    try:
        with urllib.request.urlopen(
                request, timeout=float(timeout_seconds)) as response:
            status = int(getattr(response, "status", 200))
            response.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "Home Assistant antwortet mit HTTP %d." % exc.code) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Home Assistant ist nicht erreichbar: %s" % exc.reason) from exc
    if not 200 <= status < 300:
        raise RuntimeError(
            "Home Assistant antwortet mit HTTP %d." % status)
    return {"delivered": True, "status": status}
