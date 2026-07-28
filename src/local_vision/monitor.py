"""Local webcam and vision-LLM print-failure monitor.

Printer actions are disabled unless two explicit CLI interlocks are supplied.
Camera or model failures always degrade to no printer action.
"""
import argparse
import base64
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

from .notifications import send_home_assistant_webhook


REACTIONS = ("warn", "pause", "cancel-early-pause")
ACTION_CONFIRMATION = "I_UNDERSTAND"


def validate_policy(policy):
    reaction = policy.get("reaction", "warn")
    if reaction not in REACTIONS:
        raise ValueError("Unsupported reaction: %s" % reaction)
    confidence = float(policy.get("confidence_percent", 85.0))
    consecutive = int(policy.get("consecutive_detections", 3))
    early_minutes = float(policy.get("early_cancel_minutes", 20.0))
    interval = float(policy.get("snapshot_interval_seconds", 5.0))
    if not 50.0 <= confidence <= 99.0:
        raise ValueError("Confidence must be between 50 and 99 percent")
    if not 2 <= consecutive <= 10:
        raise ValueError("Consecutive detections must be between 2 and 10")
    if not 1.0 <= early_minutes <= 240.0:
        raise ValueError("Early cancel window must be 1 to 240 minutes")
    if not 2.0 <= interval <= 60.0:
        raise ValueError("Snapshot interval must be 2 to 60 seconds")
    return {
        "reaction": reaction,
        "confidence_percent": confidence,
        "consecutive_detections": consecutive,
        "early_cancel_minutes": early_minutes,
        "snapshot_interval_seconds": interval,
    }


def decide_reaction(policy, observations, elapsed_print_seconds):
    """Return a fail-safe action from consecutive structured observations."""
    policy = validate_policy(policy)
    required = policy["consecutive_detections"]
    if len(observations) < required:
        return {"action": "none", "reason": "not_enough_observations"}
    selected = observations[-required:]
    threshold = policy["confidence_percent"] / 100.0
    if any(not item.get("image_usable", False) for item in selected):
        return {"action": "none", "reason": "image_unusable"}
    if any(float(item.get("failure_probability", 0.0)) < threshold
           for item in selected):
        return {"action": "none", "reason": "confidence_gate_not_met"}

    reaction = policy["reaction"]
    if reaction == "warn":
        action = "warn"
    elif reaction == "pause":
        action = "pause"
    elif elapsed_print_seconds <= policy["early_cancel_minutes"] * 60.0:
        action = "cancel"
    else:
        action = "pause"
    return {
        "action": action,
        "reason": "consecutive_failure_detections",
        "detections": required,
        "minimum_probability": min(
            float(item["failure_probability"]) for item in selected),
    }


def _json_request(url, method="GET", payload=None, headers=None, timeout=10):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _bytes_request(url, timeout=10):
    request = urllib.request.Request(url, headers={"Accept": "image/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        image = response.read(8 * 1024 * 1024)
    if not content_type.startswith("image/") or not image:
        raise ValueError("Snapshot endpoint did not return an image")
    return image, content_type


class VisionMonitor:
    def __init__(self, moonraker_url, printer_web_url, llm_base_url, model,
                 policy, live_status_path=None, api_token=None,
                 allow_printer_actions=False, evidence_dir=None,
                 home_assistant_webhook_url=None,
                 home_assistant_cooldown_seconds=900):
        self.moonraker_url = moonraker_url.rstrip("/")
        self.printer_web_url = printer_web_url.rstrip("/") + "/"
        self.llm_base_url = llm_base_url.rstrip("/")
        self.model = model
        self.policy = validate_policy(policy)
        self.live_status_path = live_status_path
        self.api_token = api_token
        self.allow_printer_actions = allow_printer_actions
        self.evidence_dir = evidence_dir
        self.home_assistant_webhook_url = (
            str(home_assistant_webhook_url or "").strip())
        self.home_assistant_cooldown_seconds = max(
            60.0, float(home_assistant_cooldown_seconds))
        self.observations = []
        self.action_issued = False
        self.last_notification_monotonic = None

    def _printer_status(self):
        path = "/printer/objects/query?print_stats&extruder"
        return _json_request(
            self.moonraker_url + path)["result"]["status"]

    def _snapshot(self):
        webcams = _json_request(
            self.moonraker_url + "/server/webcams/list"
        )["result"]["webcams"]
        candidates = [
            camera for camera in webcams
            if camera.get("enabled") and camera.get("snapshot_url")]
        if not candidates:
            raise RuntimeError("No enabled Moonraker snapshot camera")
        camera = candidates[0]
        snapshot_url = camera["snapshot_url"]
        if not urllib.parse.urlparse(snapshot_url).scheme:
            snapshot_url = urllib.parse.urljoin(
                self.printer_web_url, snapshot_url.lstrip("/"))
        image, content_type = _bytes_request(snapshot_url)
        return camera, image, content_type

    def _telemetry(self, status):
        print_stats = status.get("print_stats", {})
        extruder = status.get("extruder", {})
        telemetry = {
            "print_state": print_stats.get("state"),
            "filename": print_stats.get("filename"),
            "print_duration_seconds": print_stats.get("print_duration"),
            "total_duration_seconds": print_stats.get("total_duration"),
            "filament_used_mm": print_stats.get("filament_used"),
            "temperature_c": extruder.get("temperature"),
            "target_c": extruder.get("target"),
            "pressure_advance": extruder.get("pressure_advance"),
        }
        if self.live_status_path:
            try:
                with open(self.live_status_path, encoding="utf-8") as handle:
                    live = json.load(handle)
                telemetry["alps"] = live.get("force")
                telemetry["accelerometer"] = live.get("acceleration")
            except (FileNotFoundError, OSError, ValueError):
                telemetry["autopa_live_status"] = "unavailable"
        return telemetry

    def _classify(self, image, content_type, telemetry):
        encoded = base64.b64encode(image).decode("ascii")
        schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "print_failure_detection",
                "schema": {
                    "type": "object",
                    "properties": {
                        "failure_probability": {
                            "type": "number", "minimum": 0, "maximum": 1},
                        "failure_type": {"type": "string"},
                        "visible_evidence": {
                            "type": "array", "items": {"type": "string"}},
                        "image_usable": {"type": "boolean"},
                    },
                    "required": [
                        "failure_probability", "failure_type",
                        "visible_evidence", "image_usable"],
                    "additionalProperties": False,
                },
            },
        }
        payload = {
            "model": self.model,
            "temperature": 0,
            "stream": False,
            "response_format": schema,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Inspect the printer image for spaghetti, detached "
                        "parts, blobs, or printing into empty air. Return only "
                        "the requested JSON. Uncertainty lowers probability."),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Printer telemetry: %s" % json.dumps(
                                telemetry, sort_keys=True),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:%s;base64,%s"
                                % (content_type, encoded)},
                        },
                    ],
                },
            ],
        }
        headers = {}
        if self.api_token:
            headers["Authorization"] = "Bearer " + self.api_token
        result = _json_request(
            self.llm_base_url + "/chat/completions",
            method="POST", payload=payload, headers=headers, timeout=60)
        content = result["choices"][0]["message"]["content"]
        observation = json.loads(content)
        observation["model"] = self.model
        return observation

    def _store_evidence(self, image, content_type, event):
        if not self.evidence_dir:
            return None
        os.makedirs(self.evidence_dir, exist_ok=True)
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        extension = ".png" if content_type == "image/png" else ".jpg"
        image_path = os.path.join(self.evidence_dir, stamp + extension)
        event_path = os.path.join(self.evidence_dir, stamp + ".json")
        with open(image_path, "wb") as handle:
            handle.write(image)
        with open(event_path, "w", encoding="utf-8") as handle:
            json.dump(event, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return image_path

    def _apply_action(self, action):
        if action not in ("pause", "cancel"):
            return False
        if not self.allow_printer_actions or self.action_issued:
            return False
        endpoint = (
            "/printer/print/pause"
            if action == "pause" else "/printer/print/cancel")
        _json_request(
            self.moonraker_url + endpoint, method="POST", payload={})
        self.action_issued = True
        return True

    def _notify_home_assistant(self, event):
        if not self.home_assistant_webhook_url:
            return {"status": "disabled"}
        now = time.monotonic()
        if (
                self.last_notification_monotonic is not None
                and now - self.last_notification_monotonic
                < self.home_assistant_cooldown_seconds):
            return {"status": "suppressed", "reason": "cooldown"}
        observation = event["observation"]
        telemetry = event["telemetry"]
        probability = float(
            observation.get("failure_probability") or 0.0)
        failure_type = str(
            observation.get("failure_type") or "unknown")
        filename = str(telemetry.get("filename") or "Unbekannter Druck")
        action = event["decision"]["action"]
        payload = {
            "event_type": "print_failure",
            "source": "local-vision",
            "severity": "critical",
            "title": "Local Vision: Druckproblem erkannt",
            "message": (
                "%s: %s mit %.0f %% Wahrscheinlichkeit. Reaktion: %s."
                % (filename, failure_type, probability * 100.0, action)),
            "created_utc": event["created_utc"],
            "filename": filename,
            "failure_type": failure_type,
            "failure_probability": probability,
            "visible_evidence": observation.get("visible_evidence", []),
            "decision": event["decision"],
            "printer_action_executed": event["printer_action_executed"],
        }
        result = send_home_assistant_webhook(
            self.home_assistant_webhook_url,
            payload,
            timeout_seconds=10)
        self.last_notification_monotonic = now
        return {
            "status": "delivered",
            "http_status": result["status"],
        }

    def step(self):
        try:
            status = self._printer_status()
        except Exception as exc:
            return {
                "active": False,
                "action": "none",
                "reason": "moonraker_unavailable",
                "error": repr(exc),
            }
        print_stats = status.get("print_stats", {})
        if print_stats.get("state") != "printing":
            self.observations.clear()
            self.action_issued = False
            self.last_notification_monotonic = None
            return {
                "active": False,
                "action": "none",
                "reason": "printer_not_printing",
            }
        try:
            camera, image, content_type = self._snapshot()
        except Exception as exc:
            return {
                "active": True,
                "action": "none",
                "reason": "camera_unavailable_or_invalid",
                "error": repr(exc),
            }
        telemetry = self._telemetry(status)
        try:
            observation = self._classify(
                image, content_type, telemetry)
        except Exception as exc:
            return {
                "active": True,
                "action": "none",
                "reason": "model_unavailable_or_invalid",
                "error": repr(exc),
            }
        self.observations.append(observation)
        self.observations = self.observations[
            -self.policy["consecutive_detections"]:]
        decision = decide_reaction(
            self.policy, self.observations,
            float(print_stats.get("print_duration") or 0.0))
        event = {
            "created_utc": datetime.datetime.utcnow().isoformat() + "Z",
            "camera": camera.get("name"),
            "telemetry": telemetry,
            "observation": observation,
            "decision": decision,
            "printer_action_allowed": self.allow_printer_actions,
            "printer_action_executed": False,
        }
        if decision["action"] != "none":
            event["evidence_image"] = self._store_evidence(
                image, content_type, event)
            event["printer_action_executed"] = self._apply_action(
                decision["action"])
            try:
                event["home_assistant_notification"] = (
                    self._notify_home_assistant(event))
            except Exception as exc:
                event["home_assistant_notification"] = {
                    "status": "failed",
                    "error": repr(exc),
                }
        return event


def _load_console_config(path):
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            config = json.load(handle)
        return config if isinstance(config, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def main():
    parser = argparse.ArgumentParser(
        description="Monitor a print with a local camera and vision LLM")
    parser.add_argument(
        "--moonraker-url", default="http://127.0.0.1:7125")
    parser.add_argument(
        "--printer-web-url", default="http://127.0.0.1")
    parser.add_argument("--llm-base-url")
    parser.add_argument("--model")
    parser.add_argument(
        "--console-config",
        default="~/.config/local-vision-console/config.json",
        help="Reuse LLM and Home Assistant settings from the web console")
    parser.add_argument("--reaction", choices=REACTIONS, default="warn")
    parser.add_argument("--early-cancel-minutes", type=float, default=20)
    parser.add_argument(
        "--snapshot-interval-seconds", type=float, default=5)
    parser.add_argument("--confidence-percent", type=float, default=85)
    parser.add_argument("--consecutive-detections", type=int, default=3)
    parser.add_argument("--live-status")
    parser.add_argument("--evidence-dir")
    parser.add_argument("--home-assistant-webhook-url")
    parser.add_argument(
        "--home-assistant-cooldown-minutes", type=float)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--allow-printer-actions", action="store_true")
    parser.add_argument("--confirm-actions")
    args = parser.parse_args()
    if (args.allow_printer_actions
            and args.confirm_actions != ACTION_CONFIRMATION):
        parser.error(
            "--allow-printer-actions requires "
            "--confirm-actions %s" % ACTION_CONFIRMATION)
    console_config = _load_console_config(args.console_config)
    llm_base_url = args.llm_base_url or console_config.get("base_url")
    model = args.model or console_config.get("model")
    if not llm_base_url or not model:
        parser.error(
            "LLM base URL and model are required either as arguments or in "
            "--console-config")
    webhook_url = args.home_assistant_webhook_url
    if webhook_url is None:
        webhook_url = os.environ.get(
            "LOCAL_VISION_HOME_ASSISTANT_WEBHOOK_URL")
    if webhook_url is None and console_config.get("home_assistant_enabled"):
        webhook_url = console_config.get("home_assistant_webhook_url", "")
    cooldown_minutes = args.home_assistant_cooldown_minutes
    if cooldown_minutes is None:
        cooldown_minutes = float(console_config.get(
            "home_assistant_cooldown_minutes", 15))
    policy = {
        "reaction": args.reaction,
        "early_cancel_minutes": args.early_cancel_minutes,
        "snapshot_interval_seconds": args.snapshot_interval_seconds,
        "confidence_percent": args.confidence_percent,
        "consecutive_detections": args.consecutive_detections,
    }
    monitor = VisionMonitor(
        args.moonraker_url, args.printer_web_url,
        llm_base_url, model, policy,
        live_status_path=args.live_status,
        api_token=(
            os.environ.get("LOCAL_VISION_API_TOKEN")
            or console_config.get("api_key")),
        allow_printer_actions=args.allow_printer_actions,
        evidence_dir=args.evidence_dir,
        home_assistant_webhook_url=webhook_url,
        home_assistant_cooldown_seconds=cooldown_minutes * 60.0)
    while True:
        print(json.dumps(monitor.step(), indent=2, sort_keys=True))
        if args.once:
            break
        time.sleep(monitor.policy["snapshot_interval_seconds"])


if __name__ == "__main__":
    main()
