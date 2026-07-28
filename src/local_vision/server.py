"""Standalone web UI for configuring and testing a local vision LLM.

Most features are read-only. The guided camera calibration is the sole web
workflow permitted to home and move the toolhead, and requires explicit
per-run confirmation plus live Klipper safety checks.
"""

import argparse
import base64
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from .calibration import (
    CalibrationError,
    build_calibration_plan,
    project_point,
    solve_homography,
    validate_center_point,
)
from .notifications import (
    NotificationConfigurationError,
    send_home_assistant_webhook,
    validate_home_assistant_webhook_url,
)


DEFAULT_CONFIG = {
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "",
    "api_key": "",
    "timeout_seconds": 45,
    "moonraker_url": "http://127.0.0.1:7125",
    "camera_uid": "",
    "camera_view": "unknown",
    "camera_calibration": None,
    "home_assistant_webhook_url": "",
    "home_assistant_enabled": False,
    "home_assistant_cooldown_minutes": 15,
}
MAX_BODY_BYTES = 64 * 1024
COLORS = {
    "red": (235, 55, 65),
    "green": (45, 190, 90),
    "blue": (45, 105, 235),
    "yellow": (245, 205, 45),
    "magenta": (215, 55, 190),
    "cyan": (35, 195, 210),
}


class ConfigurationError(ValueError):
    """Raised when a local AI configuration is unsafe or incomplete."""


def _is_local_address(address):
    ip = ipaddress.ip_address(address)
    return ip.is_private or ip.is_loopback or ip.is_link_local


def validate_base_url(value):
    """Accept HTTP(S) endpoints that resolve only inside the local network."""
    value = str(value or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigurationError("Die URL muss mit http:// oder https:// beginnen.")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ConfigurationError("Die Server-URL ist ungültig.")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("Query und Fragment gehören nicht in die Basis-URL.")
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(parsed.hostname, parsed.port)
        }
    except socket.gaierror as exc:
        raise ConfigurationError(
            "Der Hostname der lokalen KI ist nicht auflösbar.") from exc
    if not addresses or not all(_is_local_address(item) for item in addresses):
        raise ConfigurationError(
            "Aus Sicherheitsgründen sind nur lokale/private Server erlaubt.")
    return value


def openai_url(base_url, endpoint):
    """Build an OpenAI-compatible v1 endpoint from either host or /v1 URL."""
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    return parsed._replace(
        path=path + "/" + endpoint.lstrip("/"),
        params="",
        query="",
        fragment="",
    ).geturl()


def lmstudio_metadata_url(base_url):
    """Return LM Studio's optional metadata endpoint."""
    parsed = urlparse(base_url)
    return parsed._replace(
        path="/api/v0/models",
        params="",
        query="",
        fragment="",
    ).geturl()


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path).expanduser()

    def load(self):
        config = dict(DEFAULT_CONFIG)
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                config.update({
                    key: stored[key]
                    for key in DEFAULT_CONFIG
                    if key in stored
                })
        except (FileNotFoundError, OSError, ValueError):
            pass
        return config

    def public(self):
        config = self.load()
        return {
            "baseUrl": config["base_url"],
            "model": config["model"],
            "timeoutSeconds": config["timeout_seconds"],
            "apiKeyConfigured": bool(config["api_key"]),
            "moonrakerUrl": config["moonraker_url"],
            "cameraUid": config["camera_uid"],
            "cameraView": config["camera_view"],
            "cameraCalibrationConfigured": bool(
                config["camera_calibration"]),
            "cameraCalibration": (
                {
                    "createdUtc": config["camera_calibration"].get(
                        "created_utc"),
                    "reprojectionError": config["camera_calibration"].get(
                        "reprojection_error"),
                    "camera": config["camera_calibration"].get("camera"),
                }
                if isinstance(config["camera_calibration"], dict)
                else None),
            "homeAssistantWebhookConfigured": bool(
                config["home_assistant_webhook_url"]),
            "homeAssistantEnabled": bool(config["home_assistant_enabled"]),
            "homeAssistantCooldownMinutes": int(
                config["home_assistant_cooldown_minutes"]),
        }

    def save(self, payload):
        current = self.load()
        base_url = validate_base_url(payload.get("baseUrl"))
        model = str(payload.get("model") or "").strip()
        timeout = int(payload.get("timeoutSeconds", 45))
        if not 5 <= timeout <= 180:
            raise ConfigurationError(
                "Das Zeitlimit muss zwischen 5 und 180 Sekunden liegen.")
        api_key = current["api_key"]
        if payload.get("clearApiKey"):
            api_key = ""
        elif payload.get("apiKey"):
            api_key = str(payload["apiKey"]).strip()
        moonraker_url = validate_base_url(
            payload.get("moonrakerUrl")
            or current["moonraker_url"])
        camera_uid = str(
            payload.get("cameraUid", current["camera_uid"]) or "").strip()
        camera_view = str(
            payload.get("cameraView", current["camera_view"]) or "unknown")
        allowed_views = {
            "unknown", "front", "rear", "left", "right",
            "front-left", "front-right", "rear-left", "rear-right", "top",
        }
        if camera_view not in allowed_views:
            raise ConfigurationError("Unbekannter Kamerawinkel.")
        webhook_url = current["home_assistant_webhook_url"]
        if payload.get("clearHomeAssistantWebhook"):
            webhook_url = ""
        elif payload.get("homeAssistantWebhookUrl"):
            try:
                webhook_url = validate_home_assistant_webhook_url(
                    payload["homeAssistantWebhookUrl"])
            except NotificationConfigurationError as exc:
                raise ConfigurationError(str(exc)) from exc
        home_assistant_enabled = bool(
            payload.get(
                "homeAssistantEnabled",
                current["home_assistant_enabled"]))
        if payload.get("clearHomeAssistantWebhook"):
            home_assistant_enabled = False
        if home_assistant_enabled and not webhook_url:
            raise ConfigurationError(
                "Für den Home-Assistant-Alarm muss zuerst eine Webhook-URL "
                "gespeichert werden.")
        cooldown_minutes = int(payload.get(
            "homeAssistantCooldownMinutes",
            current["home_assistant_cooldown_minutes"]))
        if not 1 <= cooldown_minutes <= 1440:
            raise ConfigurationError(
                "Die Alarm-Sperrzeit muss zwischen 1 und 1440 Minuten liegen.")
        config = {
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
            "timeout_seconds": timeout,
            "moonraker_url": moonraker_url,
            "camera_uid": camera_uid,
            "camera_view": camera_view,
            "camera_calibration": current["camera_calibration"],
            "home_assistant_webhook_url": webhook_url,
            "home_assistant_enabled": home_assistant_enabled,
            "home_assistant_cooldown_minutes": cooldown_minutes,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        return self.public()

    def save_camera_calibration(self, calibration):
        config = self.load()
        config["camera_calibration"] = calibration
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        return self.public()


def _headers(config):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if config.get("api_key"):
        headers["Authorization"] = "Bearer " + config["api_key"]
    return headers


def _json_request(url, config, method="GET", payload=None):
    data = (
        json.dumps(payload).encode("utf-8")
        if payload is not None else None)
    request = urllib.request.Request(
        url, data=data, method=method, headers=_headers(config))
    try:
        with urllib.request.urlopen(
                request, timeout=config["timeout_seconds"]) as response:
            body = response.read(4 * 1024 * 1024)
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(
            "KI-Server antwortet mit HTTP %d: %s" % (
                exc.code, detail[:500])) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "KI-Server nicht erreichbar: %s" % exc.reason) from exc
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(
            "KI-Server lieferte keine gültige JSON-Antwort.") from exc


def _binary_request(url, timeout=20, max_bytes=128 * 1024 * 1024):
    request = urllib.request.Request(
        url, headers={"Accept": "*/*", "User-Agent": "LocalVisionConsole/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise RuntimeError("Die angeforderte Datei ist zu groß.")
            return body, response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "Moonraker antwortet mit HTTP %d." % exc.code) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Moonraker oder Kamera nicht erreichbar: %s" % exc.reason) from exc


def _moonraker_json(config, path):
    base = validate_base_url(config["moonraker_url"])
    request_config = dict(config)
    # Never forward the LLM API key to Moonraker.
    request_config["api_key"] = ""
    request_config["timeout_seconds"] = min(
        int(config.get("timeout_seconds", 45)), 30)
    payload = _json_request(base + path, request_config)
    return payload.get("result", payload) if isinstance(payload, dict) else payload


def _moonraker_command(config, script, timeout=300):
    base = validate_base_url(config["moonraker_url"])
    request_config = dict(config)
    request_config["api_key"] = ""
    request_config["timeout_seconds"] = max(5, int(timeout))
    try:
        payload = _json_request(
            base + "/printer/gcode/script",
            request_config,
            method="POST",
            payload={"script": script})
    except RuntimeError as exc:
        raise RuntimeError(
            str(exc).replace("KI-Server", "Moonraker")) from exc
    return payload.get("result", payload) if isinstance(payload, dict) else payload


def _klipper_respond_available(config):
    """Return whether Klipper loaded the optional [respond] module."""
    try:
        result = _moonraker_json(
            config, "/printer/objects/query?configfile")
    except (ConfigurationError, RuntimeError):
        return False
    settings = result.get("status", result).get(
        "configfile", {}).get("settings", {})
    return isinstance(settings, dict) and "respond" in settings


def _calibration_console_message(config, message, respond_enabled):
    """Log calibration progress locally and, when available, in Mainsail."""
    clean = re.sub(r"\s+", " ", str(message)).strip()
    clean = clean.replace('"', "'")[:220]
    print("Local Vision: %s" % clean, flush=True)
    if not respond_enabled:
        return
    try:
        _moonraker_command(
            config,
            'RESPOND TYPE=echo MSG="Local Vision: %s"' % clean,
            timeout=15)
    except (ConfigurationError, RuntimeError) as exc:
        print(
            "Local Vision: Mainsail console message failed: %s" % exc,
            flush=True)


def _live_motion_state(config):
    result = _moonraker_json(
        config,
        "/printer/objects/query?toolhead&print_stats&webhooks")
    return result.get("status", result)


def _require_idle_printer(config):
    state = _live_motion_state(config)
    webhooks = state.get("webhooks", {})
    print_stats = state.get("print_stats", {})
    if webhooks.get("state") != "ready":
        raise ConfigurationError("Klipper ist nicht bereit.")
    if print_stats.get("state") not in {"standby", "complete", "cancelled"}:
        raise ConfigurationError(
            "Die Kamerakalibrierung ist nur im Leerlauf erlaubt.")
    toolhead = state.get("toolhead", {})
    try:
        plan = build_calibration_plan(
            list(toolhead.get("axis_minimum") or []),
            list(toolhead.get("axis_maximum") or []))
    except CalibrationError as exc:
        raise ConfigurationError(str(exc)) from exc
    return state, plan


def _web_root(moonraker_url):
    parsed = urlparse(moonraker_url)
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = "[%s]" % hostname
    port = "" if parsed.port in {None, 7125} else ":%d" % parsed.port
    return "%s://%s%s/" % (parsed.scheme, hostname, port)


def available_cameras(config):
    result = _moonraker_json(config, "/server/webcams/list")
    cameras = result.get("webcams", []) if isinstance(result, dict) else []
    return [{
        "uid": str(camera.get("uid") or ""),
        "name": str(camera.get("name") or "Kamera"),
        "enabled": bool(camera.get("enabled")),
        "snapshotUrl": str(camera.get("snapshot_url") or ""),
        "rotation": int(camera.get("rotation") or 0),
        "flipHorizontal": bool(camera.get("flip_horizontal")),
        "flipVertical": bool(camera.get("flip_vertical")),
    } for camera in cameras if isinstance(camera, dict)]


def _selected_camera(config):
    cameras = [
        camera for camera in available_cameras(config)
        if camera["enabled"] and camera["snapshotUrl"]
    ]
    selected_uid = config.get("camera_uid")
    if selected_uid:
        cameras = [
            camera for camera in cameras
            if camera["uid"] == selected_uid
        ]
    if not cameras:
        raise ConfigurationError(
            "Keine aktivierte Moonraker-Snapshot-Kamera gefunden.")
    return cameras[0]


def camera_snapshot(config):
    camera = _selected_camera(config)
    snapshot_url = camera["snapshotUrl"]
    if not urlparse(snapshot_url).scheme:
        snapshot_url = urljoin(
            _web_root(config["moonraker_url"]),
            snapshot_url.lstrip("/"))
    parsed_snapshot = urlparse(snapshot_url)
    validate_base_url(parsed_snapshot._replace(
        query="", fragment="").geturl())
    image, content_type = _binary_request(
        snapshot_url,
        timeout=min(int(config.get("timeout_seconds", 45)), 30),
        max_bytes=12 * 1024 * 1024)
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise RuntimeError(
            "Die Kamera lieferte kein unterstütztes Einzelbild.")
    return camera, image, content_type


def image_dimensions(image, content_type):
    if content_type == "image/png" and image.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(image) < 24:
            raise RuntimeError("Das PNG-Kamerabild ist unvollständig.")
        return struct.unpack(">II", image[16:24])
    if content_type == "image/jpeg" and image.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 <= len(image):
            if image[offset] != 0xff:
                offset += 1
                continue
            marker = image[offset + 1]
            offset += 2
            if marker in {0xd8, 0xd9} or 0xd0 <= marker <= 0xd7:
                continue
            if offset + 2 > len(image):
                break
            length = struct.unpack(">H", image[offset:offset + 2])[0]
            if length < 2 or offset + length > len(image):
                break
            if marker in {
                    0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                    0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
                if length < 7:
                    break
                height, width = struct.unpack(
                    ">HH", image[offset + 3:offset + 7])
                return width, height
            offset += length
    raise RuntimeError(
        "Die Größe des Kamerabilds konnte nicht bestimmt werden.")


def _data_url(content, content_type):
    return "data:%s;base64,%s" % (
        content_type, base64.b64encode(content).decode("ascii"))


def _parse_json_answer(answer):
    cleaned = answer.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
    except ValueError as exc:
        raise RuntimeError(
            "Das Vision-Modell lieferte keine auswertbare JSON-Antwort.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Die Winkelantwort ist kein JSON-Objekt.")
    return payload


def locate_moved_toolhead(
        config, before_image, before_type, after_image, after_type):
    """Locate the moved nozzle/toolhead in the second of two camera frames."""
    answer, latency = _chat(config, [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Zwischen Bild 1 und Bild 2 wurde ausschließlich der "
                    "Druckkopf eines stillstehenden 3D-Druckers bewegt. "
                    "Bestimme in Bild 2 möglichst die Position der Düsenspitze, "
                    "sonst den geometrischen Mittelpunkt des Druckkopfs. "
                    "Antworte ausschließlich als JSON mit x und y als "
                    "normierte Bildkoordinaten von 0 bis 1 (Ursprung links "
                    "oben), confidence von 0 bis 1, visible als Boolean und "
                    "target entweder nozzle oder toolhead. Bei Unsicherheit "
                    "visible=false. Keine Erklärung."),
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(before_image, before_type),
                },
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(after_image, after_type),
                },
            },
        ],
    }], max_tokens=120)
    result = _parse_json_answer(answer)
    try:
        x_pos = float(result.get("x"))
        y_pos = float(result.get("y"))
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Das Vision-Modell lieferte ungültige Bildkoordinaten.") from exc
    if (
            result.get("visible") is not True
            or not 0.0 <= x_pos <= 1.0
            or not 0.0 <= y_pos <= 1.0
            or not 0.0 <= confidence <= 1.0):
        raise RuntimeError(
            "Der bewegte Druckkopf wurde im Kamerabild nicht sicher erkannt.")
    if confidence < 0.65:
        raise RuntimeError(
            "Die Druckkopferkennung ist für eine Kalibrierung zu unsicher.")
    return {
        "x": x_pos,
        "y": y_pos,
        "confidence": confidence,
        "target": str(result.get("target") or "unknown"),
        "latencyMs": latency,
    }


def detect_camera_view(config):
    camera, image, content_type = camera_snapshot(config)
    answer, latency = _chat(config, [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Bestimme den groben Kamerablick auf diesen 3D-Drucker. "
                    "Antworte ausschließlich als JSON mit view (einer von "
                    "front, rear, left, right, front-left, front-right, "
                    "rear-left, rear-right, top oder unknown), confidence "
                    "(0 bis 1), bed_visible (true/false), bed_corners_visible "
                    "(0 bis 4) und note (kurzer deutscher Text). "
                    "Wenn die Orientierung nicht sicher ist, verwende unknown."),
            },
            {
                "type": "image_url",
                "image_url": {"url": _data_url(image, content_type)},
            },
        ],
    }], max_tokens=160)
    result = _parse_json_answer(answer)
    view = str(result.get("view", "unknown"))
    allowed = {
        "unknown", "front", "rear", "left", "right",
        "front-left", "front-right", "rear-left", "rear-right", "top",
    }
    if view not in allowed:
        view = "unknown"
    try:
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "ok": True,
        "camera": camera["name"],
        "suggestedView": view,
        "confidence": confidence,
        "bedVisible": bool(result.get("bed_visible")),
        "bedCornersVisible": max(
            0, min(4, int(result.get("bed_corners_visible") or 0))),
        "note": str(result.get("note") or ""),
        "latencyMs": latency,
        "requiresConfirmation": True,
    }


def _gcode_value(tokens, key, current):
    for token in tokens:
        if token.startswith(key):
            try:
                return float(token[1:])
            except ValueError:
                return current
    return current


def parse_gcode_layers(text):
    """Extract XY extrusion segments grouped by slicer layer markers."""
    x_pos = y_pos = z_pos = e_pos = 0.0
    absolute_xyz = True
    absolute_e = True
    layer = 0
    saw_layer_marker = False
    groups = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        upper = stripped.upper()
        layer_match = re.match(r";\s*LAYER\s*:\s*(-?\d+)", upper)
        if layer_match:
            layer = max(0, int(layer_match.group(1)))
            saw_layer_marker = True
            continue
        if upper.startswith(";LAYER_CHANGE"):
            layer = layer + 1 if saw_layer_marker else 0
            saw_layer_marker = True
            continue
        code = stripped.split(";", 1)[0].strip().upper()
        if not code:
            continue
        if code == "G90":
            absolute_xyz = True
            continue
        if code == "G91":
            absolute_xyz = False
            continue
        if code == "M82":
            absolute_e = True
            continue
        if code == "M83":
            absolute_e = False
            continue
        tokens = code.split()
        if not tokens:
            continue
        if tokens[0] == "G92":
            x_pos = _gcode_value(tokens[1:], "X", x_pos)
            y_pos = _gcode_value(tokens[1:], "Y", y_pos)
            z_pos = _gcode_value(tokens[1:], "Z", z_pos)
            e_pos = _gcode_value(tokens[1:], "E", e_pos)
            continue
        if tokens[0] not in {"G0", "G1"}:
            continue
        raw_x = _gcode_value(tokens[1:], "X", None)
        raw_y = _gcode_value(tokens[1:], "Y", None)
        raw_z = _gcode_value(tokens[1:], "Z", None)
        raw_e = _gcode_value(tokens[1:], "E", None)
        next_x = (
            x_pos if raw_x is None else
            raw_x if absolute_xyz else x_pos + raw_x)
        next_y = (
            y_pos if raw_y is None else
            raw_y if absolute_xyz else y_pos + raw_y)
        next_z = (
            z_pos if raw_z is None else
            raw_z if absolute_xyz else z_pos + raw_z)
        next_e = (
            e_pos if raw_e is None else
            raw_e if absolute_e else e_pos + raw_e)
        extrusion = next_e - e_pos
        if (
                extrusion > 0.0001
                and (next_x != x_pos or next_y != y_pos)):
            groups.setdefault(layer, []).append(
                (x_pos, y_pos, next_x, next_y, next_z))
        x_pos, y_pos, z_pos, e_pos = next_x, next_y, next_z, next_e
    return groups


def _set_pixel(pixels, width, height, x_pos, y_pos, color, radius=1):
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            x_value = x_pos + dx
            y_value = y_pos + dy
            if 0 <= x_value < width and 0 <= y_value < height:
                offset = (y_value * width + x_value) * 3
                pixels[offset:offset + 3] = bytes(color)


def _draw_line(pixels, width, height, start, stop, color, radius=1):
    x0, y0 = start
    x1, y1 = stop
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    step_x = 1 if x0 < x1 else -1
    step_y = 1 if y0 < y1 else -1
    error = dx + dy
    while True:
        _set_pixel(pixels, width, height, x0, y0, color, radius)
        if x0 == x1 and y0 == y1:
            return
        twice = 2 * error
        if twice >= dy:
            error += dy
            x0 += step_x
        if twice <= dx:
            error += dx
            y0 += step_y


def _rgb_png_data_url(width, height, pixels):
    rows = [
        b"\x00" + bytes(pixels[row * width * 3:(row + 1) * width * 3])
        for row in range(height)
    ]

    def chunk(kind, content):
        return (
            struct.pack(">I", len(content)) + kind + content
            + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff))

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(
            ">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b""))
    return _data_url(png, "image/png")


def render_layer_reference(
        groups, target_layer, bed_width, bed_depth,
        homography=None, image_size=None):
    if homography and image_size:
        width, height = [int(value) for value in image_size]
        if not 64 <= width <= 4096 or not 64 <= height <= 4096:
            raise RuntimeError("Die Kamerabildgröße ist ungültig.")
    else:
        width = height = 512
    pixels = bytearray((9, 12, 17) * (width * height))
    available = sorted(layer for layer, lines in groups.items() if lines)
    if not available:
        raise RuntimeError(
            "Im G-Code wurden keine Extrusionsbahnen erkannt.")
    candidates = [target_layer, target_layer - 1]
    chosen = next(
        (item for item in candidates if item in groups and groups[item]),
        min(available, key=lambda item: abs(item - target_layer)))
    margin = 18

    def point(x_value, y_value):
        if homography:
            try:
                u_pos, v_pos = project_point(
                    homography, x_value, y_value)
            except CalibrationError as exc:
                raise RuntimeError(str(exc)) from exc
            return (
                round(max(0.0, min(1.0, u_pos)) * (width - 1)),
                round(max(0.0, min(1.0, v_pos)) * (height - 1)),
            )
        return (
            margin + round(
                max(0.0, min(bed_width, x_value))
                / bed_width * (width - margin * 2)),
            height - margin - round(
                max(0.0, min(bed_depth, y_value))
                / bed_depth * (height - margin * 2)),
        )

    for layer in available:
        if layer > chosen:
            break
        color = (42, 48, 60) if layer < chosen else (88, 219, 194)
        radius = 0 if layer < chosen else 1
        for x0, y0, x1, y1, _z_pos in groups[layer]:
            _draw_line(
                pixels, width, height,
                point(x0, y0), point(x1, y1), color, radius)
    return _rgb_png_data_url(width, height, pixels), chosen


def current_gcode_reference(config, homography=None, image_size=None):
    query = (
        "/printer/objects/query?print_stats&virtual_sdcard&toolhead&configfile")
    status_result = _moonraker_json(config, query)
    status = status_result.get("status", status_result)
    print_stats = status.get("print_stats", {})
    filename = str(print_stats.get("filename") or "")
    if not filename:
        raise ConfigurationError(
            "Aktuell ist keine G-Code-Datei geladen.")
    info = print_stats.get("info") or {}
    current_layer = info.get("current_layer")
    metadata = _moonraker_json(
        config,
        "/server/files/metadata?filename=" + quote(filename))
    if current_layer is None:
        z_pos = (status.get("toolhead", {}).get("position") or [0, 0, 0])[2]
        first = float(metadata.get("first_layer_height") or 0.2)
        height = float(metadata.get("layer_height") or 0.2)
        current_layer = max(
            0, round((max(0.0, float(z_pos) - first)) / height))
    settings = status.get("configfile", {}).get("settings", {})
    bed_width = float(
        settings.get("stepper_x", {}).get("position_max") or 300)
    bed_depth = float(
        settings.get("stepper_y", {}).get("position_max") or 300)
    base = validate_base_url(config["moonraker_url"])
    gcode_url = base + "/server/files/gcodes/" + quote(filename, safe="/")
    gcode_bytes, _content_type = _binary_request(
        gcode_url,
        timeout=min(int(config.get("timeout_seconds", 45)), 60),
        max_bytes=128 * 1024 * 1024)
    groups = parse_gcode_layers(gcode_bytes.decode("utf-8", "replace"))
    reference, rendered_layer = render_layer_reference(
        groups, int(current_layer), bed_width, bed_depth,
        homography=homography, image_size=image_size)
    return {
        "filename": filename,
        "printState": print_stats.get("state"),
        "currentLayer": int(current_layer),
        "totalLayer": info.get("total_layer"),
        "renderedLayer": rendered_layer,
        "progress": status.get("virtual_sdcard", {}).get("progress"),
        "bedWidth": bed_width,
        "bedDepth": bed_depth,
        "slicer": metadata.get("slicer"),
        "layerHeight": metadata.get("layer_height"),
        "referenceImage": reference,
        "cameraAligned": bool(homography),
    }


def compare_current_print(config):
    camera, snapshot, snapshot_type = camera_snapshot(config)
    calibration = config.get("camera_calibration")
    homography = None
    image_size = None
    if (
            isinstance(calibration, dict)
            and calibration.get("camera_uid") == camera.get("uid")
            and calibration.get("homography")):
        homography = calibration["homography"]
        image_size = image_dimensions(snapshot, snapshot_type)
    reference = current_gcode_reference(
        config, homography=homography, image_size=image_size)
    reference_description = (
        "eine auf die Kameraperspektive kalibrierte Projektion"
        if reference["cameraAligned"]
        else "eine unkalibrierte Draufsicht")
    prompt = (
        "Vergleiche zwei Bilder eines laufenden FDM-Drucks. Bild 1 ist das "
        "aktuelle Kamerabild. Bild 2 ist %s der laut G-Code bis "
        "zur aktuellen Schicht erwarteten Extrusionskontur; die aktuelle "
        "Schicht ist türkis, frühere Schichten sind grau. Die Kameraansicht "
        "ist als '%s' hinterlegt. Berücksichtige die unterschiedliche "
        "Perspektive und melde keine millimetergenaue Abweichung ohne "
        "Vierpunkt-Kalibrierung. Prüfe auf abgelöstes Teil, Layer-Shift, "
        "fehlende Kontur, starke Überextrusion, Spaghetti und Druck außerhalb "
        "der erwarteten Form. Antworte auf Deutsch mit Befund, Konfidenz, "
        "möglichen Ursachen und empfohlener menschlicher Prüfung. Keine "
        "Druckerbefehle."
    ) % (
        reference_description,
        config.get("camera_view", "unknown"))
    answer, latency = _chat(config, [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(snapshot, snapshot_type),
                },
            },
            {
                "type": "image_url",
                "image_url": {"url": reference["referenceImage"]},
            },
        ],
    }], max_tokens=700)
    return {
        "ok": True,
        "filename": reference["filename"],
        "currentLayer": reference["currentLayer"],
        "renderedLayer": reference["renderedLayer"],
        "camera": camera["name"],
        "cameraView": config.get("camera_view", "unknown"),
        "cameraAligned": reference["cameraAligned"],
        "answer": answer,
        "latencyMs": latency,
        "printerAction": "none",
    }


def list_models(config):
    base_url = validate_base_url(config["base_url"])
    payload = _json_request(openai_url(base_url, "models"), config)
    model_rows = payload.get("data", []) if isinstance(payload, dict) else []
    metadata = {}
    try:
        native = _json_request(lmstudio_metadata_url(base_url), config)
        metadata = {
            item.get("id"): item
            for item in native.get("data", [])
            if isinstance(item, dict) and item.get("id")
        }
    except RuntimeError:
        # Other OpenAI-compatible servers do not expose LM Studio metadata.
        pass
    models = []
    for item in model_rows:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        extra = metadata.get(item["id"], {})
        models.append({
            "id": item["id"],
            "type": extra.get("type"),
            "architecture": extra.get("arch"),
            "state": extra.get("state"),
            "metadataSuggestsVision": extra.get("type") == "vlm",
        })
    return models


def _completion_text(payload):
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "Die Modellantwort enthält keinen Chat-Text.") from exc
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict)).strip()
    return str(content).strip()


def _chat(config, messages, max_tokens=80):
    model = str(config.get("model") or "").strip()
    if not model:
        raise ConfigurationError(
            "Bitte zuerst ein Modell auswählen und speichern.")
    started = time.monotonic()
    payload = _json_request(
        openai_url(validate_base_url(config["base_url"]),
                   "chat/completions"),
        config,
        method="POST",
        payload={
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        },
    )
    return _completion_text(payload), round(
        (time.monotonic() - started) * 1000)


def text_test(config):
    answer, latency = _chat(config, [{
        "role": "user",
        "content": (
            "Verbindungstest. Antworte ausschließlich mit dem Text TEXT_OK."),
    }], max_tokens=20)
    return {
        "ok": "TEXT_OK" in answer.upper(),
        "capability": "text",
        "answer": answer,
        "latencyMs": latency,
    }


def _png_data_url(left_rgb, right_rgb, width=128, height=64):
    rows = []
    midpoint = width // 2
    for _ in range(height):
        row = bytearray([0])
        for x_pos in range(width):
            row.extend(left_rgb if x_pos < midpoint else right_rgb)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, content):
        return (
            struct.pack(">I", len(content))
            + kind
            + content
            + struct.pack(">I", zlib.crc32(kind + content) & 0xffffffff)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _vision_color_matches(expected, observed, other_expected):
    observed = re.sub(r"[^a-z -]", " ", observed.lower())
    if re.search(r"\b%s\b" % re.escape(expected), observed):
        return True
    aliases = {
        "cyan": ("aqua", "turquoise", "teal", "light blue"),
        "magenta": ("purple", "pink"),
    }
    if any(alias in observed for alias in aliases.get(expected, ())):
        return True
    return (
        expected == "cyan"
        and other_expected != "blue"
        and re.search(r"\bblue\b", observed) is not None
    )


def vision_test(config):
    names = list(COLORS)
    left = secrets.choice(names)
    right = secrets.choice([name for name in names if name != left])
    image_url = _png_data_url(COLORS[left], COLORS[right])
    answer, latency = _chat(config, [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Betrachte das Testbild. Nenne die Farbe der linken und "
                    "der rechten Hälfte auf Englisch. Antworte kurz im Format "
                    "VISION_OK: left, right. Rate nicht."),
            },
            {
                "type": "image_url",
                "image_url": {"url": image_url},
            },
        ],
    }])
    normalized = answer.lower()
    parsed = re.search(
        r"vision_ok\s*:\s*([^,\n]+)\s*,\s*([^\n]+)",
        normalized,
    )
    ok = bool(
        parsed
        and _vision_color_matches(left, parsed.group(1), right)
        and _vision_color_matches(right, parsed.group(2), left)
    )
    return {
        "ok": ok,
        "capability": "vision",
        "expected": [left, right],
        "answer": answer,
        "latencyMs": latency,
        "explanation": (
            "Das Modell hat beide zufällig erzeugten Bildfarben erkannt."
            if ok else
            "Die Antwort stimmt nicht mit dem zufälligen Prüfbild überein."),
    }


def home_assistant_test(config):
    """Send a harmless test event through the configured HA webhook."""
    if not config.get("home_assistant_enabled"):
        raise ConfigurationError(
            "Der Home-Assistant-Alarm ist noch nicht aktiviert.")
    event = {
        "event_type": "test",
        "source": "local-vision",
        "severity": "info",
        "title": "Local Vision Testalarm",
        "message": (
            "Die Verbindung zwischen Local Vision und Home Assistant "
            "funktioniert."),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        delivered = send_home_assistant_webhook(
            config.get("home_assistant_webhook_url"),
            event,
            timeout_seconds=min(
                int(config.get("timeout_seconds", 45)), 15))
    except NotificationConfigurationError as exc:
        raise ConfigurationError(str(exc)) from exc
    return {
        "ok": True,
        "delivered": delivered["delivered"],
        "status": delivered["status"],
    }


class DatasetReader:
    """Read compact, deterministic AutoPA result files without modifying them."""

    RESULT_FILES = (
        "manifest.json",
        "quality.json",
        "analysis.json",
        "filament_analysis.json",
        "material_analysis.json",
        "temperature_analysis.json",
    )

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def list(self):
        try:
            directories = [
                item for item in self.root.iterdir()
                if item.is_dir() and not item.name.startswith(".")
            ]
        except OSError:
            return []
        rows = []
        for directory in sorted(
                directories,
                key=lambda item: item.stat().st_mtime,
                reverse=True)[:100]:
            files = [
                name for name in self.RESULT_FILES
                if (directory / name).is_file()
            ]
            rows.append({
                "id": directory.name,
                "resultFiles": files,
                "readyForLlm": (
                    "analysis.json" in files
                    or "quality.json" in files),
                "modified": int(directory.stat().st_mtime),
            })
        return rows

    def summary(self, dataset_id):
        dataset_id = str(dataset_id or "").strip()
        candidate = (self.root / dataset_id).resolve()
        if (
                not dataset_id
                or candidate.parent != self.root
                or not candidate.is_dir()):
            raise ConfigurationError("Unbekannter AutoPA-Datensatz.")
        result = {"dataset": candidate.name}
        for name in self.RESULT_FILES:
            path = candidate / name
            if not path.is_file():
                continue
            if path.stat().st_size > 256 * 1024:
                continue
            try:
                result[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                result[name] = {"warning": "Datei konnte nicht gelesen werden."}
        if len(result) == 1:
            raise ConfigurationError(
                "Der Datensatz enthält noch keine kompakten Analyseergebnisse.")
        return result


def post_print_analysis(config, dataset):
    """Ask the LLM to interpret deterministic results, never raw printer data."""
    prompt = (
        "Du bist ein vorsichtiger Analyst für FDM-Druckdaten. Bewerte die "
        "folgenden bereits deterministisch berechneten AutoPA-Ergebnisse nach "
        "dem Druck. Erfinde keine Messwerte. Wenn Daten fehlen oder Quality "
        "Gates fehlschlagen, sage klar, dass keine PA-Empfehlung zulässig ist. "
        "Antworte auf Deutsch mit den Abschnitten: Kurzfazit, Datenqualität, "
        "PA-Empfehlung, Temperatur/Material, Bewegung/Vibration, Auffälligkeiten "
        "und Nächster beaufsichtigter Test. Gib niemals G-Code aus und löse "
        "keine Druckeraktion aus.\n\nDATEN:\n"
        + json.dumps(dataset, ensure_ascii=False, separators=(",", ":"))
    )
    answer, latency = _chat(config, [{
        "role": "system",
        "content": (
            "Du analysierst Messzusammenfassungen nur beratend. Sicherheit und "
            "Datenqualität haben Vorrang vor Optimierung."),
    }, {
        "role": "user",
        "content": prompt,
    }], max_tokens=900)
    return {
        "ok": True,
        "capability": "post-print-analysis",
        "dataset": dataset["dataset"],
        "answer": answer,
        "latencyMs": latency,
        "printerAction": "none",
    }


class GuidedCalibrationManager:
    """Two-step, supervised camera calibration with strict motion gates."""

    SESSION_SECONDS = 10 * 60

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.session = None
        self.expiry_timer = None

    def _clear_session(self):
        with self.lock:
            self.session = None
            timer = self.expiry_timer
            self.expiry_timer = None
        if timer:
            timer.cancel()

    def _expire(self, token):
        with self.lock:
            if not self.session or self.session["token"] != token:
                return
            if self.session["state"] == "running":
                return
            self.session = None
            self.expiry_timer = None

    @staticmethod
    def _public_plan(state, plan):
        toolhead = state.get("toolhead", {})
        return {
            "axisMinimum": plan["axis_minimum"],
            "axisMaximum": plan["axis_maximum"],
            "bedWidth": plan["bed_width"],
            "bedDepth": plan["bed_depth"],
            "safeZ": plan["safe_z"],
            "homedAxes": str(toolhead.get("homed_axes") or ""),
            "points": [
                {
                    "name": point["name"],
                    "x": point["x"],
                    "y": point["y"],
                }
                for point in plan["points"]
            ],
        }

    def plan(self):
        config = self.store.load()
        state, plan = _require_idle_printer(config)
        return {
            "ok": True,
            "motionRequired": True,
            "homingRequired": True,
            "nozzleHeatingRequired": False,
            "nozzleCleaningRequired": False,
            "homingCommand": "G28",
            "plan": self._public_plan(state, plan),
        }

    def prepare(self, payload):
        if payload.get("motionConfirmation") != "HOME_AND_MOVE":
            raise ConfigurationError(
                "Die Bewegung wurde nicht ausdrücklich bestätigt.")
        config = self.store.load()
        if not str(config.get("model") or "").strip():
            raise ConfigurationError(
                "Vor der Kamerakalibrierung muss ein Vision-Modell "
                "konfiguriert werden.")
        _selected_camera(config)
        state, plan = _require_idle_printer(config)
        respond_enabled = _klipper_respond_available(config)
        token = secrets.token_urlsafe(24)
        with self.lock:
            if self.session:
                raise ConfigurationError(
                    "Es läuft bereits eine Kamerakalibrierung.")
            self.session = {
                "token": token,
                "state": "ready",
                "plan": plan,
                "respond_enabled": respond_enabled,
                "created_monotonic": time.monotonic(),
            }
        with self.lock:
            self.expiry_timer = threading.Timer(
                self.SESSION_SECONDS,
                self._expire,
                args=(token,))
            self.expiry_timer.daemon = True
            self.expiry_timer.start()
        return {
            "ok": True,
            "sessionToken": token,
            "state": "ready_to_home",
            "expiresSeconds": self.SESSION_SECONDS,
            "consoleMessages": respond_enabled,
            "plan": self._public_plan(state, plan),
            "message": (
                "Bereit für normales G28 ohne Heizen und die anschließende "
                "Messfahrt."),
        }

    def cancel(self, payload):
        token = str(payload.get("sessionToken") or "")
        config = self.store.load()
        with self.lock:
            if not self.session or self.session["token"] != token:
                raise ConfigurationError(
                    "Keine passende Kalibrierung gefunden.")
            if self.session["state"] == "running":
                raise ConfigurationError(
                    "Die Messfahrt läuft bereits. Bei Gefahr Not-Aus benutzen.")
        self._clear_session()
        return {"ok": True, "state": "cancelled"}

    def run(self, payload):
        token = str(payload.get("sessionToken") or "")
        if payload.get("motionConfirmation") != "HOME_AND_MOVE":
            raise ConfigurationError(
                "Homing und Druckkopfbewegung wurden nicht bestätigt.")
        with self.lock:
            if not self.session or self.session["token"] != token:
                raise ConfigurationError(
                    "Die Kalibrierung ist abgelaufen oder unbekannt.")
            if self.session["state"] != "ready":
                raise ConfigurationError(
                    "Die Kalibrierung ist nicht bereit für die Messfahrt.")
            if (
                    time.monotonic() - self.session["created_monotonic"]
                    > self.SESSION_SECONDS):
                raise ConfigurationError("Die Kalibrierung ist abgelaufen.")
            self.session["state"] = "running"
            plan = self.session["plan"]
            respond_enabled = self.session.get("respond_enabled", False)
            timer = self.expiry_timer
            self.expiry_timer = None
        if timer:
            timer.cancel()
        config = self.store.load()
        observations = []
        success = False
        try:
            _require_idle_printer(config)
            _calibration_console_message(
                config,
                "Kamerakalibrierung: Homing wird gestartet "
                "(G28, ohne Heizen).",
                respond_enabled)
            _moonraker_command(config, "G28", timeout=240)
            state, current_plan = _require_idle_printer(config)
            homed_axes = str(
                state.get("toolhead", {}).get("homed_axes") or "")
            if not all(axis in homed_axes for axis in "xyz"):
                raise RuntimeError(
                    "Klipper bestätigt nach G28 nicht alle drei Achsen.")
            if (
                    current_plan["axis_minimum"] != plan["axis_minimum"]
                    or current_plan["axis_maximum"] != plan["axis_maximum"]):
                raise RuntimeError(
                    "Die Achsgrenzen haben sich während der Kalibrierung "
                    "geändert.")
            center = plan["points"][-1]
            speed = plan["travel_speed_mm_s"] * 60.0
            _calibration_console_message(
                config,
                "Homing abgeschlossen. Sichere Ausgangsposition "
                "wird angefahren.",
                respond_enabled)
            _moonraker_command(
                config,
                (
                    "G90\n"
                    "G0 Z%.3f F600\n"
                    "G0 X%.3f Y%.3f F%.0f\n"
                    "M400\nG4 P700"
                ) % (
                    plan["safe_z"],
                    center["x"],
                    center["y"],
                    speed),
                timeout=90)
            camera, before_image, before_type = camera_snapshot(config)
            for point_number, point in enumerate(plan["points"], start=1):
                _require_idle_printer(config)
                _calibration_console_message(
                    config,
                    "Messpunkt %d/%d %s wird angefahren: X%.1f Y%.1f."
                    % (
                        point_number,
                        len(plan["points"]),
                        point["name"],
                        point["x"],
                        point["y"]),
                    respond_enabled)
                _moonraker_command(
                    config,
                    "G90\nG0 X%.3f Y%.3f F%.0f\nM400\nG4 P700" % (
                        point["x"], point["y"], speed),
                    timeout=90)
                _camera, after_image, after_type = camera_snapshot(config)
                located = locate_moved_toolhead(
                    config,
                    before_image,
                    before_type,
                    after_image,
                    after_type)
                observations.append({
                    "name": point["name"],
                    "bed": [point["x"], point["y"]],
                    "image": [located["x"], located["y"]],
                    "confidence": located["confidence"],
                    "target": located["target"],
                    "latency_ms": located["latencyMs"],
                })
                _calibration_console_message(
                    config,
                    "Messpunkt %d/%d erkannt, Konfidenz %d Prozent."
                    % (
                        point_number,
                        len(plan["points"]),
                        round(located["confidence"] * 100)),
                    respond_enabled)
                before_image, before_type = after_image, after_type
            _calibration_console_message(
                config,
                "Alle Messpunkte erfasst. Kameraprojektion wird geprüft.",
                respond_enabled)
            targets = {item["target"] for item in observations}
            if len(targets) != 1:
                raise RuntimeError(
                    "Das Modell hat Düse und Druckkopf uneinheitlich "
                    "lokalisiert.")
            homography = solve_homography(
                [tuple(item["bed"]) for item in observations[:4]],
                [tuple(item["image"]) for item in observations[:4]])
            reprojection_error = validate_center_point(
                homography,
                tuple(observations[4]["bed"]),
                tuple(observations[4]["image"]))
            if reprojection_error > 0.08:
                raise RuntimeError(
                    "Die Kontrollposition weicht zu stark von der "
                    "Kameraprojektion ab.")
            calibration = {
                "created_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "camera": camera["name"],
                "camera_uid": camera["uid"],
                "homography": homography,
                "reprojection_error": reprojection_error,
                "minimum_confidence": min(
                    item["confidence"] for item in observations),
                "target": next(iter(targets)),
                "axis_minimum": plan["axis_minimum"],
                "axis_maximum": plan["axis_maximum"],
                "safe_z": plan["safe_z"],
                "points": observations,
            }
            self.store.save_camera_calibration(calibration)
            _calibration_console_message(
                config,
                "Kalibrierung gespeichert. Druckkopf fährt zur sicheren "
                "Mittelposition zurück.",
                respond_enabled)
            _moonraker_command(
                config,
                "G90\nG0 Z%.3f F600\nG0 X%.3f Y%.3f F%.0f\nM400" % (
                    plan["safe_z"],
                    center["x"],
                    center["y"],
                    speed),
                timeout=90)
            success = True
            _calibration_console_message(
                config,
                "Kamerakalibrierung erfolgreich abgeschlossen.",
                respond_enabled)
            return {
                "ok": True,
                "state": "calibrated",
                "camera": camera["name"],
                "reprojectionError": reprojection_error,
                "minimumConfidence": calibration["minimum_confidence"],
                "samples": observations,
            }
        except Exception as exc:
            _calibration_console_message(
                config,
                "Kamerakalibrierung abgebrochen: %s" % exc,
                respond_enabled)
            raise
        finally:
            self._clear_session()
            if not success:
                print("Camera calibration aborted without enabling a heater.")


def make_handler(
        store, static_dir, dataset_reader=None, calibration_manager=None):
    static_root = Path(static_dir).resolve()
    calibration_manager = (
        calibration_manager or GuidedCalibrationManager(store))

    class LocalVisionHandler(BaseHTTPRequestHandler):
        server_version = "LocalVisionConsole/0.1"

        def _json(self, payload, status=200):
            body = json.dumps(
                payload, ensure_ascii=False,
                separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _request_json(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ConfigurationError(
                    "Ungültige Anfragegröße.") from exc
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ConfigurationError(
                    "Die Anfrage ist leer oder zu groß.")
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise ConfigurationError(
                    "Die Anfrage enthält kein gültiges JSON.") from exc

        def _run(self, callback):
            try:
                self._json(callback())
            except (ConfigurationError, CalibrationError) as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            except RuntimeError as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            except Exception:
                self._json({
                    "ok": False,
                    "error": "Unerwarteter Fehler im lokalen KI-Test.",
                }, 500)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/health":
                self._json({
                    "ok": True,
                    "service": "local-vision-console",
                    "printerControl": True,
                    "printerControlScope": "guided-camera-calibration-only",
                    "automaticPrintActions": False,
                    "autopaDependency": False,
                })
                return
            if path == "/api/config":
                self._json(store.public())
                return
            if path == "/api/models":
                self._run(lambda: {
                    "ok": True,
                    "models": list_models(store.load()),
                })
                return
            if path == "/api/datasets":
                self._json({
                    "ok": True,
                    "datasets": (
                        dataset_reader.list() if dataset_reader else []),
                })
                return
            if path == "/api/cameras":
                self._run(lambda: {
                    "ok": True,
                    "cameras": available_cameras(store.load()),
                })
                return
            if path == "/api/camera/calibration/plan":
                self._run(calibration_manager.plan)
                return
            relative = path.lstrip("/") or "index.html"
            candidate = (static_root / relative).resolve()
            if static_root not in candidate.parents and candidate != static_root:
                self.send_error(403)
                return
            if not candidate.is_file():
                self.send_error(404)
                return
            body = candidate.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(candidate.name)[0]
                or "application/octet-stream")
            self.send_header(
                "Cache-Control",
                "no-cache, no-store, must-revalidate")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/config":
                self._run(lambda: store.save(self._request_json()))
                return
            if path == "/api/test/text":
                self._run(lambda: text_test(store.load()))
                return
            if path == "/api/test/vision":
                self._run(lambda: vision_test(store.load()))
                return
            if path == "/api/home-assistant/test":
                self._run(lambda: home_assistant_test(store.load()))
                return
            if path == "/api/analyze":
                def analyze():
                    if not dataset_reader:
                        raise ConfigurationError(
                            "Kein AutoPA-Datenordner konfiguriert.")
                    payload = self._request_json()
                    return post_print_analysis(
                        store.load(),
                        dataset_reader.summary(payload.get("dataset")))
                self._run(analyze)
                return
            if path == "/api/camera/detect-view":
                self._run(lambda: detect_camera_view(store.load()))
                return
            if path == "/api/camera/calibration/prepare":
                self._run(lambda: calibration_manager.prepare(
                    self._request_json()))
                return
            if path == "/api/camera/calibration/run":
                self._run(lambda: calibration_manager.run(
                    self._request_json()))
                return
            if path == "/api/camera/calibration/cancel":
                self._run(lambda: calibration_manager.cancel(
                    self._request_json()))
                return
            if path == "/api/reference/compare":
                self._run(lambda: compare_current_print(store.load()))
                return
            self._json({"ok": False, "error": "Unbekannter Endpunkt."}, 404)

        def log_message(self, message, *args):
            print("%s - %s" % (self.address_string(), message % args))

    return LocalVisionHandler


def main():
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Standalone local vision LLM configuration UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7127)
    parser.add_argument(
        "--config",
        default=os.path.expanduser(
            "~/.config/local-vision-console/config.json"))
    parser.add_argument(
        "--static-dir",
        default=str(project_root / "web"))
    parser.add_argument(
        "--autopa-data",
        default=os.path.expanduser("~/printer_data/autopa"),
        help=(
            "Optional read-only AutoPA result directory for post-print "
            "analysis"))
    args = parser.parse_args()
    store = ConfigStore(args.config)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(
            store,
            args.static_dir,
            DatasetReader(args.autopa_data)))
    print("Local Vision Console listening on http://%s:%d" % (
        args.host, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
