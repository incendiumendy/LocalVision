"""Standalone web UI for configuring and testing a local vision LLM.

Most features are read-only. The guided camera calibration is the sole web
workflow permitted to home and move the toolhead, and requires explicit
per-run confirmation plus live Klipper safety checks.
"""

import argparse
import base64
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import statistics
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


class ToolheadLocationError(RuntimeError):
    """Preserve a raw vision answer when coordinate validation fails."""

    def __init__(self, message, raw_answer):
        super().__init__(message)
        self.raw_answer = str(raw_answer)


class ToolheadNotVisibleError(ToolheadLocationError):
    """Raised when deterministic tracking confirms an empty camera frame."""


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
    except TimeoutError as exc:
        raise RuntimeError(
            "KI-Server antwortet nicht innerhalb von %d Sekunden."
            % int(config["timeout_seconds"])) from exc
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


def _require_cold_idle_printer(config):
    """Require a motionless, cold printer before loose filament is handled."""
    result = _moonraker_json(
        config,
        "/printer/objects/query?"
        "print_stats&webhooks&extruder&heater_bed&motion_report")
    state = result.get("status", result)
    if state.get("webhooks", {}).get("state") != "ready":
        raise ConfigurationError("Klipper ist nicht bereit.")
    print_state = state.get("print_stats", {}).get("state")
    if print_state not in {"standby", "complete", "cancelled"}:
        raise ConfigurationError(
            "Der Spaghetti-Test ist nur bei stillstehendem Drucker erlaubt.")
    live_velocity = float(
        state.get("motion_report", {}).get("live_velocity") or 0.0)
    if abs(live_velocity) > 0.1:
        raise ConfigurationError(
            "Der Drucker bewegt sich noch; der Spaghetti-Test wurde gesperrt.")
    temperatures = {}
    for name, maximum in (("extruder", 45.0), ("heater_bed", 45.0)):
        heater = state.get(name, {})
        temperature = float(heater.get("temperature") or 0.0)
        target = float(heater.get("target") or 0.0)
        if target > 0.0:
            raise ConfigurationError(
                "Für den Spaghetti-Test müssen alle Heizungsziele 0 sein.")
        if temperature >= maximum:
            raise ConfigurationError(
                "%s ist mit %.1f °C noch zu warm."
                % (
                    "Die Düse" if name == "extruder" else "Das Druckbett",
                    temperature))
        temperatures[name] = temperature
    return {
        "printState": print_state,
        "extruderTemperature": temperatures["extruder"],
        "bedTemperature": temperatures["heater_bed"],
        "heaterTargetsZero": True,
        "liveVelocity": live_velocity,
    }


def mainsail_theme(config):
    """Read the live Mainsail/RatOS primary color from the Moonraker DB."""
    try:
        payload = _moonraker_json(
            config,
            "/server/database/item"
            "?namespace=mainsail&key=uiSettings.primary")
    except (ConfigurationError, RuntimeError):
        return {"primary": None, "source": "unavailable"}
    value = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(value, str):
        return {"primary": None, "source": "default"}
    value = value.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", value):
        return {"primary": None, "source": "default"}
    return {"primary": value, "source": "mainsail"}


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


def locate_toolhead_in_frame(config, image, image_type):
    """Locate the toolhead bounding-box center in one camera frame."""
    answer, latency = _chat(config, [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Markiere in diesem einzelnen Kamerabild ausschließlich "
                    "die deutlich sichtbare bewegliche Druckkopf-Verkleidung "
                    "des 3D-Druckers. Ignoriere Bett, Gantry, Schläuche und "
                    "Kabel. Die Verkleidung kann farbig sein; im aktuellen "
                    "Aufbau ist sie häufig grün. Gib ihren engsten "
                    "Begrenzungsrahmen auf einer Skala von 0 bis 1000 aus, "
                    "mit Ursprung links oben. Antworte ausschließlich als "
                    "JSON: "
                    "{\"bbox\":[x1,y1,x2,y2],\"confidence\":0.0,"
                    "\"visible\":true,\"target\":\"toolhead\"}. "
                    "Keine Erklärung."),
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(image, image_type),
                },
            },
        ],
    }], max_tokens=100)
    result = _parse_json_answer(answer)
    try:
        bbox = result.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise TypeError("bbox missing")
        x1, y1, x2, y2 = (float(value) for value in bbox)
        if max(abs(value) for value in (x1, y1, x2, y2)) > 1.0:
            x1, y1, x2, y2 = (
                value / 1000.0 for value in (x1, y1, x2, y2))
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ToolheadLocationError(
            "Das Vision-Modell lieferte keinen gültigen "
            "Druckkopf-Begrenzungsrahmen.",
            answer) from exc
    if (
            result.get("visible") is not True
            or not 0.0 <= x1 < x2 <= 1.0
            or not 0.0 <= y1 < y2 <= 1.0
            or not 0.0 <= confidence <= 1.0):
        raise ToolheadLocationError(
            "Der Druckkopf wurde im Kamerabild nicht sicher erkannt.",
            answer)
    if confidence < 0.65:
        raise ToolheadLocationError(
            "Die Druckkopferkennung ist für eine Kalibrierung zu unsicher.",
            answer)
    if x2 - x1 < 0.03 or y2 - y1 < 0.03:
        raise ToolheadLocationError(
            "Der erkannte Druckkopf-Begrenzungsrahmen ist zu klein.",
            answer)
    return {
        "x": (x1 + x2) / 2.0,
        "y": (y1 + y2) / 2.0,
        "bbox": [x1, y1, x2, y2],
        "confidence": confidence,
        "target": str(result.get("target") or "toolhead"),
        "latencyMs": latency,
        "rawAnswer": answer,
    }


def learn_toolhead_color_tracker(image, image_type, bbox):
    """Learn a saturated toolhead hue from an LLM-confirmed image crop."""
    try:
        import cv2
        import numpy
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV und NumPy werden für die sichere "
            "Druckkopfverfolgung benötigt.") from exc
    frame = cv2.imdecode(
        numpy.frombuffer(image, dtype=numpy.uint8),
        cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError(
            "Das Kamerabild konnte für die Druckkopfverfolgung "
            "nicht dekodiert werden.")
    height, width = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    crop = frame[
        max(0, round(y1 * height)):min(height, round(y2 * height)),
        max(0, round(x1 * width)):min(width, round(x2 * width)),
    ]
    if crop.size == 0:
        raise RuntimeError(
            "Der bestätigte Druckkopfrahmen ist im Kamerabild leer.")
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    valid = (hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 35)
    valid_count = int(numpy.count_nonzero(valid))
    if valid_count < max(100, round(valid.size * 0.05)):
        raise RuntimeError(
            "Die Druckkopf-Verkleidung besitzt für eine sichere "
            "Farbverfolgung zu wenig unterscheidbare Farbe.")
    histogram = numpy.bincount(
        hsv[:, :, 0][valid], minlength=180)
    hue = int(numpy.argmax(histogram))
    return {
        "method": "learned-hue",
        "hue": hue,
        "hueTolerance": 12,
        "minimumAreaRatio": 0.003,
    }


def locate_toolhead_by_color(image, image_type, tracker):
    """Locate the learned toolhead color or report that it is out of frame."""
    del image_type
    try:
        import cv2
        import numpy
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV und NumPy werden für die sichere "
            "Druckkopfverfolgung benötigt.") from exc
    started = time.monotonic()
    frame = cv2.imdecode(
        numpy.frombuffer(image, dtype=numpy.uint8),
        cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError(
            "Das Kamerabild konnte für die Druckkopfverfolgung "
            "nicht dekodiert werden.")
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hue = int(tracker["hue"])
    distance = numpy.abs(
        hsv[:, :, 0].astype("int16") - hue)
    distance = numpy.minimum(distance, 180 - distance)
    mask = (
        (distance <= int(tracker.get("hueTolerance", 12)))
        & (hsv[:, :, 1] > 90)
        & (hsv[:, :, 2] > 30)
    ).astype("uint8") * 255
    kernel = numpy.ones((7, 7), dtype="uint8")
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    height, width = frame.shape[:2]
    minimum_area = (
        width * height
        * float(tracker.get("minimumAreaRatio", 0.003)))
    candidates = []
    for index in range(1, count):
        x_pos, y_pos, box_width, box_height, area = stats[index]
        if area < minimum_area:
            continue
        candidates.append((
            int(area),
            [
                x_pos / width,
                y_pos / height,
                (x_pos + box_width) / width,
                (y_pos + box_height) / height,
            ],
        ))
    if not candidates:
        raise ToolheadNotVisibleError(
            "Der Druckkopf ist an dieser Bettposition nicht vollständig "
            "im Kamerabild sichtbar.",
            json.dumps({
                "method": "learned-hue",
                "hue": hue,
                "visible": False,
            }, sort_keys=True))
    area, bbox = max(candidates, key=lambda item: item[0])
    x1, y1, x2, y2 = bbox
    return {
        "x": (x1 + x2) / 2.0,
        "y": (y1 + y2) / 2.0,
        "bbox": bbox,
        "confidence": min(0.999, area / max(minimum_area * 2.0, 1.0)),
        "target": "toolhead",
        "latencyMs": round((time.monotonic() - started) * 1000),
        "rawAnswer": json.dumps({
            "method": "learned-hue",
            "hue": hue,
            "visible": True,
            "area": area,
            "bbox": bbox,
        }, sort_keys=True),
    }


def toolhead_bbox_has_camera_margin(location, margin=0.01):
    """Require the tracked toolhead to stay clear of all image borders."""
    x1, y1, x2, y2 = location["bbox"]
    return (
        x1 >= margin
        and y1 >= margin
        and x2 <= 1.0 - margin
        and y2 <= 1.0 - margin
    )


def locate_moved_toolhead(
        config, before_image, before_type, after_image, after_type):
    """Locate the moved nozzle/toolhead in the second of two camera frames."""
    answer, latency = _chat(config, [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Vergleiche die beiden direkt folgenden Kamerabilder "
                    "pixelgenau. BILD 1 ist VORHER, BILD 2 ist NACHHER. "
                    "Zwischen beiden Bildern wurde ausschließlich der "
                    "Druckkopf eines stillstehenden 3D-Druckers bewegt. "
                    "Lokalisiere in beiden Bildern dieselbe Düsenspitze; nur "
                    "wenn diese nicht erkennbar ist, denselben geometrischen "
                    "Mittelpunkt des Druckkopfs. Antworte ausschließlich als "
                    "JSON mit before und after als Objekte mit x und y, "
                    "confidence von 0 bis 1, visible_before und visible_after "
                    "als Boolean und target entweder nozzle oder toolhead. "
                    "x und y sind normierte Bildkoordinaten von 0 bis 1 mit "
                    "Ursprung links oben. Gib keine geschätzte Bildmitte aus. "
                    "Bei Unsicherheit setze beide visible-Werte auf false. "
                    "Keine Erklärung."),
            },
            {
                "type": "text",
                "text": "BILD 1 – VORHER",
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": _data_url(before_image, before_type),
                },
            },
            {
                "type": "text",
                "text": "BILD 2 – NACHHER",
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
        before = result.get("before")
        after = result.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise TypeError("before/after missing")
        before_x = float(before.get("x"))
        before_y = float(before.get("y"))
        after_x = float(after.get("x"))
        after_y = float(after.get("y"))
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ToolheadLocationError(
            "Das Vision-Modell lieferte keine gültigen Vorher-/Nachher-"
            "Koordinaten.",
            answer) from exc
    if (
            result.get("visible_before") is not True
            or result.get("visible_after") is not True
            or not 0.0 <= before_x <= 1.0
            or not 0.0 <= before_y <= 1.0
            or not 0.0 <= after_x <= 1.0
            or not 0.0 <= after_y <= 1.0
            or not 0.0 <= confidence <= 1.0):
        raise ToolheadLocationError(
            "Der bewegte Druckkopf wurde im Kamerabild nicht sicher erkannt.",
            answer)
    if confidence < 0.65:
        raise ToolheadLocationError(
            "Die Druckkopferkennung ist für eine Kalibrierung zu unsicher.",
            answer)
    motion = math.hypot(after_x - before_x, after_y - before_y)
    if motion < 0.02:
        raise ToolheadLocationError(
            "Das Vision-Modell erkennt zwischen Vorher- und Nachher-Bild "
            "keine ausreichende Druckkopfbewegung.",
            answer)
    return {
        "x": after_x,
        "y": after_y,
        "beforeX": before_x,
        "beforeY": before_y,
        "confidence": confidence,
        "target": str(result.get("target") or "unknown"),
        "latencyMs": latency,
        "motion": motion,
        "rawAnswer": answer,
    }


def aggregate_toolhead_locations(locations):
    """Median three or more LLM locations and reject unstable samples."""
    if len(locations) < 3:
        raise RuntimeError(
            "Für einen Messpunkt werden mindestens drei Analysen benötigt.")
    targets = {str(item.get("target") or "unknown") for item in locations}
    if len(targets) != 1 or "unknown" in targets:
        raise RuntimeError(
            "Das Modell hat Düse und Druckkopf innerhalb eines Messpunkts "
            "uneinheitlich lokalisiert.")
    x_pos = statistics.median(float(item["x"]) for item in locations)
    y_pos = statistics.median(float(item["y"]) for item in locations)
    before_x = statistics.median(
        float(item["beforeX"]) for item in locations)
    before_y = statistics.median(
        float(item["beforeY"]) for item in locations)
    confidence = statistics.median(
        float(item["confidence"]) for item in locations)
    spread = max(
        math.hypot(float(item["x"]) - x_pos, float(item["y"]) - y_pos)
        for item in locations)
    before_spread = max(
        math.hypot(
            float(item["beforeX"]) - before_x,
            float(item["beforeY"]) - before_y)
        for item in locations)
    motion = math.hypot(x_pos - before_x, y_pos - before_y)
    if spread > 0.06 or before_spread > 0.06:
        raise RuntimeError(
            "Die wiederholte Druckkopferkennung streut zu stark.")
    if motion < 0.02:
        raise RuntimeError(
            "Die wiederholte Analyse erkennt keine ausreichende "
            "Druckkopfbewegung.")
    return {
        "x": x_pos,
        "y": y_pos,
        "beforeX": before_x,
        "beforeY": before_y,
        "confidence": confidence,
        "target": next(iter(targets)),
        "latencyMs": round(statistics.median(
            float(item["latencyMs"]) for item in locations)),
        "motion": motion,
        "spread": spread,
        "beforeSpread": before_spread,
        "sampleCount": len(locations),
    }


def validate_calibration_observations(observations):
    """Reject duplicate or nearly collinear image points before solving."""
    if len(observations) < 2:
        return None
    for index, current in enumerate(observations):
        for previous in observations[:index]:
            distance = math.hypot(
                current["image"][0] - previous["image"][0],
                current["image"][1] - previous["image"][1])
            if distance < 0.025:
                raise RuntimeError(
                    "Die Bildpositionen %s und %s sind nicht eindeutig "
                    "(Abstand %.1f Prozent)."
                    % (
                        previous["name"],
                        current["name"],
                        distance * 100.0))
    if len(observations) < 4:
        return None
    corners = observations[:4]
    area = abs(sum(
        corners[index]["image"][0]
        * corners[(index + 1) % 4]["image"][1]
        - corners[(index + 1) % 4]["image"][0]
        * corners[index]["image"][1]
        for index in range(4))) / 2.0
    if area < 0.01:
        raise RuntimeError(
            "Die erkannten Eckpunkte decken im Kamerabild zu wenig Fläche "
            "ab und sind für eine Projektion nicht eindeutig.")
    return area


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


def _calibration_llm_config(config):
    """Give cold local vision models enough time without changing user config."""
    request_config = dict(config)
    request_config["timeout_seconds"] = max(
        180, int(config.get("timeout_seconds", 45)))
    return request_config


def _model_server_url(config, endpoint):
    base = validate_base_url(config["base_url"])
    parsed = urlparse(base)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    path = path.rstrip("/") + "/" + endpoint.lstrip("/")
    return parsed._replace(
        path=path, params="", query="", fragment="").geturl()


def _llama_router_model_status(config):
    """Return router status or None for a fixed single-model server."""
    request_config = _calibration_llm_config(config)
    request_config["timeout_seconds"] = min(
        request_config["timeout_seconds"], 30)
    try:
        payload = _json_request(
            _model_server_url(config, "models"), request_config)
    except RuntimeError:
        return None
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    configured = str(config.get("model") or "").strip()
    for row in rows:
        if not isinstance(row, dict):
            continue
        names = {
            str(row.get("id") or ""),
            str(row.get("name") or ""),
            str(row.get("model") or ""),
        }
        aliases = row.get("aliases")
        if isinstance(aliases, list):
            names.update(str(item) for item in aliases)
        if configured not in names:
            continue
        status = row.get("status")
        return status if isinstance(status, dict) else None
    return None


def ensure_model_loaded(config):
    """Load router-managed models; fixed servers wake on the warm-up request."""
    status = _llama_router_model_status(config)
    if status is None:
        return {"manager": "fixed-or-idle", "loadRequested": False}
    if status.get("value") == "loaded":
        return {"manager": "llama-router", "loadRequested": False}
    request_config = _calibration_llm_config(config)
    _json_request(
        _model_server_url(config, "models/load"),
        request_config,
        method="POST",
        payload={"model": str(config["model"])})
    for _ in range(180):
        status = _llama_router_model_status(config)
        if status and status.get("value") == "loaded":
            return {"manager": "llama-router", "loadRequested": True}
        if status and status.get("failed"):
            raise RuntimeError(
                "Das Vision-Modell konnte nicht geladen werden.")
        time.sleep(1)
    raise RuntimeError(
        "Das Vision-Modell wurde nicht innerhalb von 180 Sekunden geladen.")


def warmup_vision_model(config):
    """Run a tiny multimodal request before any printer movement."""
    request_config = _calibration_llm_config(config)
    image_url = _png_data_url(
        COLORS["cyan"], COLORS["magenta"], width=32, height=16)
    answer, latency = _chat(request_config, [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Dies ist ein technischer Vision-Warm-up. Betrachte das "
                    "kleine Bild und antworte ausschließlich mit "
                    "VISION_READY."),
            },
            {
                "type": "image_url",
                "image_url": {"url": image_url},
            },
        ],
    }], max_tokens=12)
    if "VISION_READY" not in answer.upper():
        raise RuntimeError(
            "Das Vision-Modell hat den Warm-up nicht bestätigt.")
    return {
        "ok": True,
        "latencyMs": latency,
        "timeoutSeconds": request_config["timeout_seconds"],
    }


def unload_model_if_supported(config):
    """Immediately unload router models; fixed servers use idle sleep."""
    status = _llama_router_model_status(config)
    if status is None:
        return False
    if status.get("value") == "unloaded":
        return True
    request_config = _calibration_llm_config(config)
    request_config["timeout_seconds"] = min(
        request_config["timeout_seconds"], 30)
    _json_request(
        _model_server_url(config, "models/unload"),
        request_config,
        method="POST",
        payload={"model": str(config["model"])})
    return True


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
    ANALYSES_PER_POINT = 3

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.session = None
        self.expiry_timer = None

    def _new_diagnostic_run(self, token, plan):
        created_utc = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        run_name = time.strftime(
            "%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + token[:8]
        root = self.store.path.parent / "calibration-runs"
        run_dir = root / run_name
        run_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(root, 0o700)
        os.chmod(run_dir, 0o700)
        metadata = {
            "created_utc": created_utc,
            "state": "running",
            "analyses_per_point": self.ANALYSES_PER_POINT,
            "plan": plan,
            "points": [],
        }
        self._write_diagnostic_metadata(run_dir, metadata)
        return run_dir, metadata

    @staticmethod
    def _write_diagnostic_metadata(run_dir, metadata):
        path = run_dir / "metadata.json"
        temporary = run_dir / "metadata.tmp"
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    @staticmethod
    def _write_diagnostic_image(
            run_dir, stem, image, content_type):
        extensions = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        extension = extensions.get(content_type)
        if not extension:
            raise RuntimeError(
                "Diagnosebild hat ein nicht unterstütztes Format.")
        path = run_dir / (stem + extension)
        path.write_bytes(image)
        os.chmod(path, 0o600)
        return path.name

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

    @staticmethod
    def _fit_plan_to_camera(
            config, plan, camera, tracker, image_size,
            respond_enabled):
        """Move safe candidate points inward until the toolhead is visible."""
        center = plan["points"][-1]
        speed = plan["travel_speed_mm_s"] * 60.0
        adjusted = []
        _calibration_console_message(
            config,
            "Sichtbarer Kamerabereich wird ohne weitere LLM-Abfragen "
            "automatisch geprüft.",
            respond_enabled)

        def probe(candidate, label):
            _require_idle_printer(config)
            _moonraker_command(
                config,
                "G90\nG0 X%.3f Y%.3f F%.0f\nM400\nG4 P500" % (
                    candidate["x"],
                    candidate["y"],
                    speed),
                timeout=90)
            current_camera, image, image_type = camera_snapshot(config)
            if current_camera["uid"] != camera["uid"]:
                raise RuntimeError(
                    "Die aktive Kamera hat sich während der "
                    "Sichtbereichsprüfung geändert.")
            if image_dimensions(image, image_type) != image_size:
                raise RuntimeError(
                    "Die Kamerabildgröße hat sich während der "
                    "Sichtbereichsprüfung geändert.")
            try:
                location = locate_toolhead_by_color(
                    image, image_type, tracker)
            except ToolheadNotVisibleError:
                _calibration_console_message(
                    config,
                    "%s bei X%.1f Y%.1f liegt außerhalb des "
                    "Kamerabilds."
                    % (label, candidate["x"], candidate["y"]),
                    respond_enabled)
                return None
            if not toolhead_bbox_has_camera_margin(location):
                _calibration_console_message(
                    config,
                    "%s bei X%.1f Y%.1f ist am Bildrand abgeschnitten."
                    % (label, candidate["x"], candidate["y"]),
                    respond_enabled)
                return None
            return location

        for point_index, requested in enumerate(plan["points"][:4]):
            if point_index < 2:
                x_anchor = center["x"]
                y_anchor = center["y"]
            else:
                front_anchor = adjusted[1 if point_index == 2 else 0]
                x_anchor = center["x"]
                y_anchor = front_anchor["y"]
            accepted = None
            for factor in (1.0, 0.8, 0.6, 0.4, 0.2, 0.1):
                candidate = {
                    "name": requested["name"],
                    "x": (
                        x_anchor
                        + (requested["x"] - x_anchor) * factor),
                    "y": (
                        y_anchor
                        + (requested["y"] - y_anchor) * factor),
                }
                if probe(candidate, requested["name"]) is not None:
                    accepted = candidate
                    if factor < 1.0:
                        _calibration_console_message(
                            config,
                            "%s wird auf den sichtbaren Punkt X%.1f Y%.1f "
                            "eingerückt."
                            % (
                                requested["name"],
                                candidate["x"],
                                candidate["y"]),
                            respond_enabled)
                    break
            if accepted is None:
                raise RuntimeError(
                    "Für %s wurde auch im verkleinerten Kalibrierbereich "
                    "keine vollständig sichtbare Druckkopfposition gefunden."
                    % requested["name"])
            adjusted.append(accepted)

        centroid = {
            "name": "center",
            "x": statistics.mean(point["x"] for point in adjusted),
            "y": statistics.mean(point["y"] for point in adjusted),
        }
        check_candidates = [
            dict(center),
            centroid,
            {
                "name": "center",
                "x": (centroid["x"] * 0.75 + adjusted[0]["x"] * 0.25),
                "y": (centroid["y"] * 0.75 + adjusted[0]["y"] * 0.25),
            },
        ]
        check = None
        check_location = None
        for candidate in check_candidates:
            location = probe(candidate, "Kontrollpunkt")
            if location is not None:
                check = candidate
                check_location = location
                break
        if check is None:
            raise RuntimeError(
                "Im Inneren der sichtbaren Kalibrierfläche wurde kein "
                "vollständig sichtbarer Kontrollpunkt gefunden.")
        adjusted.append(check)
        fitted = dict(plan)
        fitted["points"] = adjusted
        return fitted, check_location

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
            "analysesPerPoint": self.ANALYSES_PER_POINT,
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
        _calibration_console_message(
            config,
            "Vision-Modell wird vor jeder Druckerbewegung geladen und "
            "vorgewärmt.",
            respond_enabled)
        lifecycle = ensure_model_loaded(config)
        warmup = warmup_vision_model(config)
        _calibration_console_message(
            config,
            "Vision-Modell ist bereit (Warm-up %.1f Sekunden)."
            % (warmup["latencyMs"] / 1000.0),
            respond_enabled)
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
                "model_lifecycle": lifecycle,
                "model_warmup": warmup,
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
            "analysesPerPoint": self.ANALYSES_PER_POINT,
            "modelWarmupMs": warmup["latencyMs"],
            "modelManager": lifecycle["manager"],
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
        vision_config = _calibration_llm_config(config)
        observations = []
        success = False
        diagnostic_run = None
        diagnostic_metadata = None
        try:
            diagnostic_run, diagnostic_metadata = self._new_diagnostic_run(
                token, plan)
            _calibration_console_message(
                config,
                "Diagnosebilder und Koordinaten werden gespeichert: %s"
                % diagnostic_run,
                respond_enabled)
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
            initial_dimensions = image_dimensions(before_image, before_type)
            diagnostic_metadata["camera"] = {
                "name": camera["name"],
                "uid": camera["uid"],
                "image_size": list(initial_dimensions),
            }
            diagnostic_metadata["initial_image"] = (
                self._write_diagnostic_image(
                    diagnostic_run,
                    "00-initial-before",
                    before_image,
                    before_type))
            _calibration_console_message(
                config,
                "Ausgangsposition des Druckkopfs wird im Kamerabild "
                "erkannt.",
                respond_enabled)
            try:
                previous_location = locate_toolhead_in_frame(
                    vision_config,
                    before_image,
                    before_type)
            except Exception as exc:
                initial_detection = {"error": str(exc)}
                raw_answer = getattr(exc, "raw_answer", None)
                if raw_answer is not None:
                    initial_detection["raw_answer"] = raw_answer
                diagnostic_metadata["initial_detection"] = (
                    initial_detection)
                self._write_diagnostic_metadata(
                    diagnostic_run, diagnostic_metadata)
                raise
            diagnostic_metadata["initial_detection"] = {
                "image": [
                    previous_location["x"],
                    previous_location["y"],
                ],
                "bbox": previous_location["bbox"],
                "confidence": previous_location["confidence"],
                "target": previous_location["target"],
                "latency_ms": previous_location["latencyMs"],
                "raw_answer": previous_location["rawAnswer"],
            }
            tracker = learn_toolhead_color_tracker(
                before_image,
                before_type,
                previous_location["bbox"])
            diagnostic_metadata["toolhead_tracker"] = tracker
            requested_plan = plan
            plan, previous_location = self._fit_plan_to_camera(
                config,
                plan,
                camera,
                tracker,
                initial_dimensions,
                respond_enabled)
            diagnostic_metadata["requested_plan"] = requested_plan
            diagnostic_metadata["plan"] = plan
            self._write_diagnostic_metadata(
                diagnostic_run, diagnostic_metadata)
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
                analyses = []
                diagnostic_point = {
                    "name": point["name"],
                    "bed": [point["x"], point["y"]],
                    "analyses": [],
                }
                diagnostic_metadata["points"].append(diagnostic_point)
                self._write_diagnostic_metadata(
                    diagnostic_run, diagnostic_metadata)
                after_image = None
                after_type = None
                for analysis_number in range(
                        1, self.ANALYSES_PER_POINT + 1):
                    if analysis_number > 1:
                        time.sleep(0.25)
                    current_camera, after_image, after_type = camera_snapshot(
                        config)
                    if current_camera["uid"] != camera["uid"]:
                        raise RuntimeError(
                            "Die aktive Kamera hat sich während der "
                            "Kalibrierung geändert.")
                    if image_dimensions(after_image, after_type) != (
                            initial_dimensions):
                        raise RuntimeError(
                            "Die Kamerabildgröße hat sich während der "
                            "Kalibrierung geändert.")
                    image_name = self._write_diagnostic_image(
                        diagnostic_run,
                        "%02d-%s-after-%d" % (
                            point_number,
                            point["name"],
                            analysis_number),
                        after_image,
                        after_type)
                    diagnostic_sample = {
                        "analysis": analysis_number,
                        "image": image_name,
                    }
                    try:
                        current_location = locate_toolhead_by_color(
                            after_image,
                            after_type,
                            tracker)
                        motion = math.hypot(
                            current_location["x"]
                            - previous_location["x"],
                            current_location["y"]
                            - previous_location["y"])
                        if motion < 0.02:
                            raise ToolheadLocationError(
                                "Die Einbild-Erkennung erkennt keine "
                                "ausreichende Druckkopfbewegung.",
                                current_location["rawAnswer"])
                        sample = {
                            "x": current_location["x"],
                            "y": current_location["y"],
                            "beforeX": previous_location["x"],
                            "beforeY": previous_location["y"],
                            "confidence": current_location["confidence"],
                            "target": current_location["target"],
                            "latencyMs": current_location["latencyMs"],
                            "motion": motion,
                            "rawAnswer": current_location["rawAnswer"],
                        }
                    except Exception as exc:
                        diagnostic_sample["error"] = str(exc)
                        raw_answer = getattr(exc, "raw_answer", None)
                        if raw_answer is not None:
                            diagnostic_sample["raw_answer"] = raw_answer
                        diagnostic_point["analyses"].append(
                            diagnostic_sample)
                        self._write_diagnostic_metadata(
                            diagnostic_run, diagnostic_metadata)
                        raise
                    analyses.append(sample)
                    diagnostic_sample.update({
                        "analysis": analysis_number,
                        "image": image_name,
                        "before": [
                            sample["beforeX"],
                            sample["beforeY"],
                        ],
                        "after": [sample["x"], sample["y"]],
                        "confidence": sample["confidence"],
                        "target": sample["target"],
                        "motion": sample["motion"],
                        "bbox": current_location["bbox"],
                        "latency_ms": sample["latencyMs"],
                        "raw_answer": sample["rawAnswer"],
                    })
                    diagnostic_point["analyses"].append(
                        diagnostic_sample)
                    self._write_diagnostic_metadata(
                        diagnostic_run, diagnostic_metadata)
                    _calibration_console_message(
                        config,
                        (
                            "Messpunkt %d/%d, Analyse %d/%d: vorher "
                            "(%.3f, %.3f), nachher (%.3f, %.3f), "
                            "Konfidenz %d Prozent."
                        ) % (
                            point_number,
                            len(plan["points"]),
                            analysis_number,
                            self.ANALYSES_PER_POINT,
                            sample["beforeX"],
                            sample["beforeY"],
                            sample["x"],
                            sample["y"],
                            round(sample["confidence"] * 100)),
                        respond_enabled)
                located = aggregate_toolhead_locations(analyses)
                observation = {
                    "name": point["name"],
                    "bed": [point["x"], point["y"]],
                    "image": [located["x"], located["y"]],
                    "before_image": [
                        located["beforeX"],
                        located["beforeY"],
                    ],
                    "confidence": located["confidence"],
                    "target": located["target"],
                    "latency_ms": located["latencyMs"],
                    "motion": located["motion"],
                    "spread": located["spread"],
                    "sample_count": located["sampleCount"],
                }
                observations.append(observation)
                diagnostic_point.update(observation)
                self._write_diagnostic_metadata(
                    diagnostic_run, diagnostic_metadata)
                validate_calibration_observations(observations)
                _calibration_console_message(
                    config,
                    (
                        "Messpunkt %d/%d stabil: Bildposition "
                        "(%.3f, %.3f), Streuung %.1f Prozent, "
                        "Bewegung %.1f Prozent."
                    )
                    % (
                        point_number,
                        len(plan["points"]),
                        located["x"],
                        located["y"],
                        located["spread"] * 100.0,
                        located["motion"] * 100.0),
                    respond_enabled)
                previous_location = located
            _calibration_console_message(
                config,
                "Alle Messpunkte erfasst. Kameraprojektion wird geprüft.",
                respond_enabled)
            covered_area = validate_calibration_observations(observations)
            diagnostic_metadata["normalized_corner_area"] = covered_area
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
                "diagnostic_run": diagnostic_run.name,
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
            diagnostic_metadata["state"] = "calibrated"
            diagnostic_metadata["reprojection_error"] = reprojection_error
            diagnostic_metadata["calibration"] = {
                "target": calibration["target"],
                "minimum_confidence": calibration["minimum_confidence"],
                "normalized_corner_area": covered_area,
            }
            self._write_diagnostic_metadata(
                diagnostic_run, diagnostic_metadata)
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
                "diagnosticRun": diagnostic_run.name,
            }
        except Exception as exc:
            if diagnostic_run and diagnostic_metadata:
                diagnostic_metadata["state"] = "failed"
                diagnostic_metadata["error"] = str(exc)
                try:
                    self._write_diagnostic_metadata(
                        diagnostic_run, diagnostic_metadata)
                except OSError as diagnostic_exc:
                    print(
                        "Local Vision: diagnostic metadata failed: %s"
                        % diagnostic_exc,
                        flush=True)
            _calibration_console_message(
                config,
                "Kamerakalibrierung abgebrochen: %s" % exc,
                respond_enabled)
            raise
        finally:
            self._clear_session()
            try:
                if unload_model_if_supported(config):
                    _calibration_console_message(
                        config,
                        "Vision-Modell wurde nach der Kalibrierung "
                        "entladen.",
                        respond_enabled)
                else:
                    _calibration_console_message(
                        config,
                        "Vision-Modell wird durch den llama.cpp-Idle-Schlaf "
                        "automatisch entladen.",
                        respond_enabled)
            except Exception as release_exc:
                print(
                    "Local Vision: model unload failed: %s" % release_exc,
                    flush=True)
            if not success:
                print("Camera calibration aborted without enabling a heater.")


def _spaghetti_image_difference(
        reference_image, reference_type, current_image, current_type):
    """Return a compact deterministic difference summary for two frames."""
    del reference_type, current_type
    try:
        import cv2
        import numpy
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV und NumPy werden für den Spaghetti-Test benötigt.") from exc
    reference = cv2.imdecode(
        numpy.frombuffer(reference_image, dtype=numpy.uint8),
        cv2.IMREAD_COLOR)
    current = cv2.imdecode(
        numpy.frombuffer(current_image, dtype=numpy.uint8),
        cv2.IMREAD_COLOR)
    if (
            reference is None
            or current is None
            or reference.shape != current.shape):
        raise RuntimeError(
            "Referenz- und Prüfbild haben kein vergleichbares Format.")
    reference_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
    current_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    difference = cv2.absdiff(reference_gray, current_gray)
    difference = cv2.GaussianBlur(difference, (5, 5), 0)
    _, mask = cv2.threshold(difference, 24, 255, cv2.THRESH_BINARY)
    kernel = numpy.ones((5, 5), dtype="uint8")
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    changed_pixels = int(cv2.countNonZero(mask))
    height, width = mask.shape
    changed_ratio = changed_pixels / float(width * height)
    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bbox = None
    if contours:
        x_pos, y_pos, box_width, box_height = cv2.boundingRect(
            max(contours, key=cv2.contourArea))
        bbox = [
            x_pos / width,
            y_pos / height,
            (x_pos + box_width) / width,
            (y_pos + box_height) / height,
        ]
    return {
        "changedPixelRatio": changed_ratio,
        "changedPixels": changed_pixels,
        "largestChangeBox": bbox,
        "imageSize": [width, height],
    }


def _analyze_spaghetti_frame(config, image, image_type, difference):
    """Classify loose filament in one frame; never issue printer commands."""
    hint = difference.get("largestChangeBox")
    answer, latency = _chat(_calibration_llm_config(config), [{
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "Look at this single camera image of a cold and stationary "
                    "3D printer. Are loose tangled filament strands, commonly "
                    "called a spaghetti print failure, clearly visible on the "
                    "print bed? Ignore the toolhead, cables, shadows and bed "
                    "texture. The largest changed area from a separate clean "
                    "reference comparison is %s. Answer with exactly one word "
                    "and no explanation: SPAGHETTI if visible, otherwise CLEAN.")
                % json.dumps(hint),
            },
            {
                "type": "image_url",
                "image_url": {"url": _data_url(image, image_type)},
            },
        ],
    }], max_tokens=20)
    tokens = set(re.findall(r"\b(?:SPAGHETTI|CLEAN)\b", answer.upper()))
    if len(tokens) != 1:
        raise RuntimeError(
            "Das Vision-Modell lieferte keine eindeutige "
            "Spaghetti-Entscheidung.")
    detected = "SPAGHETTI" in tokens
    changed_ratio = float(difference.get("changedPixelRatio") or 0.0)
    difference_score = min(1.0, max(0.0, changed_ratio / 0.01))
    confidence = round(
        0.65 + 0.30 * difference_score if detected
        else 0.65 + 0.25 * (1.0 - difference_score),
        3)
    if hint:
        x_pos = (float(hint[0]) + float(hint[2])) / 2.0
        y_pos = (float(hint[1]) + float(hint[3])) / 2.0
    else:
        x_pos = None
        y_pos = None
    return {
        "spaghettiDetected": detected,
        "confidence": confidence,
        "confidenceSource": "binary-model-plus-reference-difference",
        "description": (
            "Lose Filamentfäden erkannt." if detected
            else "Keine losen Filamentfäden erkannt."),
        "x": x_pos,
        "y": y_pos,
        "latencyMs": latency,
        "rawAnswer": answer,
    }


class SpaghettiTestManager:
    """Two-stage, cold and motionless loose-filament test workflow."""

    SESSION_SECONDS = 20 * 60

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.session = None
        self.expiry_timer = None

    @staticmethod
    def _write_private(path, content):
        path.write_bytes(content)
        os.chmod(path, 0o600)

    def _clear(self):
        with self.lock:
            self.session = None
            timer = self.expiry_timer
            self.expiry_timer = None
        if timer:
            timer.cancel()

    def _expire(self, token):
        with self.lock:
            if self.session and self.session["token"] == token:
                self.session = None
                self.expiry_timer = None

    def status(self):
        with self.lock:
            if not self.session:
                return {
                    "ok": True,
                    "state": "idle",
                    "motionCommands": False,
                    "heaterCommands": False,
                }
            if self.session["state"] == "preparing":
                return {
                    "ok": True,
                    "state": "preparing",
                    "motionCommands": False,
                    "heaterCommands": False,
                }
            return {
                "ok": True,
                "state": self.session["state"],
                "sessionToken": self.session["token"],
                "createdUtc": self.session["created_utc"],
                "camera": self.session["camera"]["name"],
                "safety": self.session["safety"],
                "motionCommands": False,
                "heaterCommands": False,
            }

    def prepare(self, payload):
        if payload.get("confirmation") != "COLD_IDLE_REFERENCE":
            raise ConfigurationError(
                "Der kalte, stillstehende Referenztest wurde nicht "
                "ausdrücklich bestätigt.")
        token = secrets.token_urlsafe(24)
        with self.lock:
            if self.session:
                raise ConfigurationError(
                    "Es ist bereits ein Spaghetti-Test vorbereitet.")
            self.session = {"token": token, "state": "preparing"}
        try:
            config = self.store.load()
            safety = _require_cold_idle_printer(config)
            camera, image, image_type = camera_snapshot(config)
            dimensions = image_dimensions(image, image_type)
            run_name = (
                time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                + "-" + token[:8])
            root = self.store.path.parent / "spaghetti-tests"
            run_dir = root / run_name
            run_dir.mkdir(parents=True, exist_ok=False)
            os.chmod(root, 0o700)
            os.chmod(run_dir, 0o700)
            extension = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
            }[image_type]
            reference_path = run_dir / ("reference" + extension)
            self._write_private(reference_path, image)
            metadata = {
                "created_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "state": "awaiting_spaghetti",
                "camera": camera,
                "image_size": list(dimensions),
                "reference_image": reference_path.name,
                "safety": safety,
                "motion_commands": False,
                "heater_commands": False,
            }
            metadata_path = run_dir / "metadata.json"
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            os.chmod(metadata_path, 0o600)
            with self.lock:
                self.session = {
                    "token": token,
                    "state": "awaiting_spaghetti",
                    "created_utc": metadata["created_utc"],
                    "camera": camera,
                    "image_size": dimensions,
                    "image_type": image_type,
                    "reference_image": image,
                    "run_dir": run_dir,
                    "metadata": metadata,
                    "safety": safety,
                }
                self.expiry_timer = threading.Timer(
                    self.SESSION_SECONDS, self._expire, args=(token,))
                self.expiry_timer.daemon = True
                self.expiry_timer.start()
        except Exception:
            with self.lock:
                if self.session and self.session["token"] == token:
                    self.session = None
            raise
        print(
            "Local Vision: Kalte Spaghetti-Referenz aufgenommen; "
            "wartet auf manuell aufgelegtes Filament.",
            flush=True)
        return {
            **self.status(),
            "message": (
                "Referenz gespeichert. Jetzt Filament-Spaghetti auf das "
                "kalte Druckbett legen und danach die Prüfung starten."),
            "expiresSeconds": self.SESSION_SECONDS,
        }

    def analyze(self, payload):
        token = str(payload.get("sessionToken") or "")
        config = self.store.load()
        safety = _require_cold_idle_printer(config)
        with self.lock:
            session = self.session
            if (
                    not session
                    or session["token"] != token
                    or session["state"] != "awaiting_spaghetti"):
                raise ConfigurationError(
                    "Keine passende vorbereitete Spaghetti-Referenz gefunden.")
            session["state"] = "analyzing"
        completed = False
        try:
            camera, image, image_type = camera_snapshot(config)
            if camera["uid"] != session["camera"]["uid"]:
                raise RuntimeError(
                    "Die aktive Kamera hat sich seit der Referenz geändert.")
            if image_dimensions(image, image_type) != session["image_size"]:
                raise RuntimeError(
                    "Die Kamerabildgröße hat sich seit der Referenz geändert.")
            difference = _spaghetti_image_difference(
                session["reference_image"],
                session["image_type"],
                image,
                image_type)
            extension = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
            }[image_type]
            current_path = session["run_dir"] / ("spaghetti" + extension)
            self._write_private(current_path, image)
            ensure_model_loaded(config)
            warmup = warmup_vision_model(config)
            analysis = _analyze_spaghetti_frame(
                config, image, image_type, difference)
            detected = bool(
                difference["changedPixelRatio"] >= 0.001
                and analysis["spaghettiDetected"]
                and analysis["confidence"] >= 0.65)
            result = {
                "ok": True,
                "state": "complete",
                "spaghettiDetected": detected,
                "confidence": analysis["confidence"],
                "description": analysis["description"],
                "difference": difference,
                "analysis": analysis,
                "modelWarmupMs": warmup["latencyMs"],
                "safety": safety,
                "motionCommands": False,
                "heaterCommands": False,
            }
            result_path = session["run_dir"] / "result.json"
            result_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            os.chmod(result_path, 0o600)
            print(
                "Local Vision: Spaghetti-Test abgeschlossen: %s."
                % ("erkannt" if detected else "nicht erkannt"),
                flush=True)
            completed = True
            return result
        except Exception:
            with self.lock:
                if self.session and self.session["token"] == token:
                    self.session["state"] = "awaiting_spaghetti"
            raise
        finally:
            try:
                unload_model_if_supported(config)
            except Exception as exc:
                print(
                    "Local Vision: model unload after spaghetti test "
                    "failed: %s" % exc,
                    flush=True)
            if completed:
                self._clear()

    def cancel(self, payload):
        token = str(payload.get("sessionToken") or "")
        with self.lock:
            if not self.session or self.session["token"] != token:
                raise ConfigurationError(
                    "Keine passende Spaghetti-Referenz gefunden.")
        self._clear()
        return {
            "ok": True,
            "state": "cancelled",
            "motionCommands": False,
            "heaterCommands": False,
        }


def make_handler(
        store, static_dir, dataset_reader=None, calibration_manager=None,
        spaghetti_manager=None):
    static_root = Path(static_dir).resolve()
    calibration_manager = (
        calibration_manager or GuidedCalibrationManager(store))
    spaghetti_manager = (
        spaghetti_manager or SpaghettiTestManager(store))

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
            if path == "/api/spaghetti/status":
                self._run(spaghetti_manager.status)
                return
            if path == "/api/theme":
                self._run(lambda: mainsail_theme(store.load()))
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
            if path == "/api/spaghetti/prepare":
                self._run(lambda: spaghetti_manager.prepare(
                    self._request_json()))
                return
            if path == "/api/spaghetti/analyze":
                self._run(lambda: spaghetti_manager.analyze(
                    self._request_json()))
                return
            if path == "/api/spaghetti/cancel":
                self._run(lambda: spaghetti_manager.cancel(
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
