import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from local_vision.server import (
    ConfigStore,
    DEFAULT_CONFIG,
    DatasetReader,
    GuidedCalibrationManager,
    _png_data_url,
    _vision_color_matches,
    image_dimensions,
    make_handler,
    openai_url,
    parse_gcode_layers,
    render_layer_reference,
    validate_base_url,
)


class LocalVisionConsoleTests(unittest.TestCase):
    def test_openai_url_accepts_host_or_v1_base(self):
        self.assertEqual(
            "http://127.0.0.1:1234/v1/models",
            openai_url("http://127.0.0.1:1234", "models"))
        self.assertEqual(
            "http://127.0.0.1:1234/v1/chat/completions",
            openai_url(
                "http://127.0.0.1:1234/v1", "chat/completions"))

    def test_public_config_never_returns_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory, "config.json"))
            with mock.patch(
                    "local_vision.server.socket.getaddrinfo",
                    return_value=[
                        (None, None, None, None, ("192.168.1.20", 1234)),
                    ]):
                public = store.save({
                    "baseUrl": "http://192.168.1.20:1234/v1",
                    "model": "vision-model",
                    "apiKey": "top-secret",
                    "timeoutSeconds": 30,
                    "homeAssistantWebhookUrl": (
                        "http://192.168.1.20:8123/api/webhook/secret-id"),
                    "homeAssistantEnabled": True,
                    "homeAssistantCooldownMinutes": 15,
                })
                cleared = store.save({
                    "baseUrl": "http://192.168.1.20:1234/v1",
                    "model": "vision-model",
                    "timeoutSeconds": 30,
                    "clearHomeAssistantWebhook": True,
                    "homeAssistantEnabled": True,
                })
            self.assertTrue(public["apiKeyConfigured"])
            self.assertTrue(public["homeAssistantWebhookConfigured"])
            self.assertTrue(public["homeAssistantEnabled"])
            self.assertNotIn("api_key", public)
            self.assertNotIn("top-secret", json.dumps(public))
            self.assertNotIn("secret-id", json.dumps(public))
            self.assertFalse(cleared["homeAssistantWebhookConfigured"])
            self.assertFalse(cleared["homeAssistantEnabled"])

    def test_rejects_public_ai_endpoint(self):
        with mock.patch(
                "local_vision.server.socket.getaddrinfo",
                return_value=[
                    (None, None, None, None, ("8.8.8.8", 443)),
                ]):
            with self.assertRaises(ValueError):
                validate_base_url("https://example.com/v1")

    def test_generated_vision_probe_is_png(self):
        value = _png_data_url((255, 0, 0), (0, 0, 255))
        self.assertTrue(value.startswith("data:image/png;base64,"))

    def test_common_vision_color_synonyms_are_accepted_safely(self):
        self.assertTrue(_vision_color_matches("cyan", "blue", "green"))
        self.assertTrue(_vision_color_matches("cyan", "turquoise", "red"))
        self.assertTrue(_vision_color_matches("magenta", "purple", "yellow"))
        self.assertFalse(_vision_color_matches("cyan", "blue", "blue"))
        self.assertFalse(_vision_color_matches("green", "blue", "red"))

    def test_gcode_layer_is_rendered_as_reference_image(self):
        layers = parse_gcode_layers(
            "G90\nM82\n"
            ";LAYER:0\n"
            "G1 X10 Y10 E0\n"
            "G1 X20 Y10 E1\n"
            ";LAYER:1\n"
            "G1 X20 Y20 E2\n"
            "G1 X10 Y20 E3\n")
        self.assertEqual(1, len(layers[0]))
        self.assertEqual(2, len(layers[1]))
        image, chosen = render_layer_reference(
            layers, 1, bed_width=100, bed_depth=100)
        self.assertEqual(1, chosen)
        self.assertTrue(image.startswith("data:image/png;base64,"))

    def test_gcode_reference_can_be_projected_into_camera_view(self):
        layers = parse_gcode_layers(
            "G90\nM82\n;LAYER:0\n"
            "G1 X60 Y60 E0\nG1 X240 Y60 E1\n")
        homography = [
            [1 / 300, 0, 0],
            [0, 1 / 300, 0],
            [0, 0, 1],
        ]
        image, chosen = render_layer_reference(
            layers, 0, 300, 300,
            homography=homography, image_size=(640, 480))
        self.assertEqual(0, chosen)
        self.assertTrue(image.startswith("data:image/png;base64,"))

    def test_png_dimensions_are_read_without_image_dependency(self):
        data_url = _png_data_url((255, 0, 0), (0, 0, 255))
        import base64
        image = base64.b64decode(data_url.split(",", 1)[1])
        self.assertEqual((128, 64), image_dimensions(image, "image/png"))

    def test_dataset_reader_only_loads_compact_results(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory, "capture-1")
            dataset.mkdir()
            Path(dataset, "analysis.json").write_text(
                '{"recommended_pa": 0.035}', encoding="utf-8")
            Path(dataset, "force.csv").write_text(
                "raw,values\n1,2\n", encoding="utf-8")
            reader = DatasetReader(directory)
            rows = reader.list()
            self.assertTrue(rows[0]["readyForLlm"])
            summary = reader.summary("capture-1")
            self.assertIn("analysis.json", summary)
            self.assertNotIn("force.csv", summary)

    def test_health_declares_independence_from_printer(self):
        with tempfile.TemporaryDirectory() as directory:
            static_dir = Path(directory, "web")
            static_dir.mkdir()
            Path(static_dir, "index.html").write_text(
                "<!doctype html><title>Local Vision</title>",
                encoding="utf-8")
            store = ConfigStore(Path(directory, "config.json"))
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(store, static_dir))
            thread = threading.Thread(
                target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%d" % server.server_port
            try:
                with urllib.request.urlopen(
                        base + "/api/health") as response:
                    payload = json.load(response)
                self.assertTrue(payload["printerControl"])
                self.assertEqual(
                    "guided-camera-calibration-only",
                    payload["printerControlScope"])
                self.assertFalse(payload["automaticPrintActions"])
                self.assertFalse(payload["autopaDependency"])
                with urllib.request.urlopen(base + "/") as response:
                    self.assertIn(b"Local Vision", response.read())
                request = urllib.request.Request(
                    base + "/api/test/unknown",
                    method="POST",
                    data=b"{}",
                    headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request)
                self.assertEqual(404, raised.exception.code)
                raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_ui_exposes_automatic_active_camera_option(self):
        project = Path(__file__).resolve().parents[1]
        html = Path(
            project,
            "web",
            "index.html",
        ).read_text(encoding="utf-8")
        script = Path(
            project,
            "web",
            "app.js",
        ).read_text(encoding="utf-8")
        self.assertIn("Automatisch · aktive Moonraker-Kamera", html)
        self.assertIn("← Klipper", html)
        self.assertNotIn("← AutoPA", html)
        self.assertIn("Automatisch · ${activeCamera.name} (aktiv)", script)
        self.assertIn("loadCameras(false)", script)
        self.assertIn('url.port === "7127"', script)
        self.assertIn('"/local-vision/"', script)
        self.assertIn("Home Assistant Alarm", html)
        self.assertIn('"/api/home-assistant/test"', script)
        self.assertIn("Auto-Kalibrierung starten", html)
        self.assertIn('class="panel calibration-panel"', html)
        self.assertEqual(1, html.count('id="auto-calibrate-camera"'))
        self.assertLess(
            html.index("Automatische Kamerakalibrierung"),
            html.index("Home Assistant Alarm"))
        self.assertIn("app.js?v=8", html)
        self.assertIn("styles.css?v=8", html)
        self.assertIn("zusätzlich in der Mainsail-Konsole", html)
        self.assertIn('"/api/camera/calibration/run"', script)

    def test_guided_calibration_homes_without_heating_and_saves_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "config.json")
            config = dict(DEFAULT_CONFIG)
            config.update({
                "model": "vision-model",
                "camera_uid": "cam-1",
            })
            config_path.write_text(
                json.dumps(config), encoding="utf-8")
            store = ConfigStore(config_path)
            manager = GuidedCalibrationManager(store)
            plan = {
                "axis_minimum": [0.0, 0.0, -5.0],
                "axis_maximum": [300.0, 300.0, 300.0],
                "bed_width": 300.0,
                "bed_depth": 300.0,
                "safe_z": 20.0,
                "travel_speed_mm_s": 50.0,
                "points": [
                    {"name": "front-left", "x": 60.0, "y": 60.0},
                    {"name": "front-right", "x": 240.0, "y": 60.0},
                    {"name": "rear-right", "x": 240.0, "y": 240.0},
                    {"name": "rear-left", "x": 60.0, "y": 240.0},
                    {"name": "center", "x": 150.0, "y": 150.0},
                ],
            }
            state = {
                "toolhead": {
                    "homed_axes": "xyz",
                    "axis_minimum": [0.0, 0.0, -5.0, 0.0],
                    "axis_maximum": [300.0, 300.0, 300.0, 0.0],
                },
                "print_stats": {"state": "standby"},
                "webhooks": {"state": "ready"},
            }
            locations = [
                {"x": 0.2, "y": 0.2},
                {"x": 0.8, "y": 0.2},
                {"x": 0.8, "y": 0.8},
                {"x": 0.2, "y": 0.8},
                {"x": 0.5, "y": 0.5},
            ]
            located = [
                {
                    **point,
                    "confidence": 0.95,
                    "target": "nozzle",
                    "latencyMs": 10,
                }
                for point in locations
            ]
            camera = {"uid": "cam-1", "name": "cam1"}
            with ExitStack() as stack:
                stack.enter_context(mock.patch(
                    "local_vision.server._selected_camera",
                    return_value=camera))
                stack.enter_context(mock.patch(
                    "local_vision.server._require_idle_printer",
                    return_value=(state, plan)))
                stack.enter_context(mock.patch(
                    "local_vision.server._klipper_respond_available",
                    return_value=True))
                command = stack.enter_context(mock.patch(
                    "local_vision.server._moonraker_command"))
                stack.enter_context(mock.patch(
                    "local_vision.server.camera_snapshot",
                    return_value=(camera, b"image", "image/jpeg")))
                stack.enter_context(mock.patch(
                    "local_vision.server.locate_moved_toolhead",
                    side_effect=located))
                prepared = manager.prepare({
                    "motionConfirmation": "HOME_AND_MOVE",
                })
                result = manager.run({
                    "sessionToken": prepared["sessionToken"],
                    "motionConfirmation": "HOME_AND_MOVE",
                })
            scripts = [
                call.args[1] for call in command.call_args_list
            ]
            motion_scripts = [
                script for script in scripts
                if not script.startswith("RESPOND ")]
            console_scripts = [
                script for script in scripts
                if script.startswith("RESPOND ")]
            self.assertEqual("G28", motion_scripts[0])
            self.assertFalse(any("HEATER" in script for script in scripts))
            self.assertFalse(any("PREPARE_HOME" in script for script in scripts))
            self.assertTrue(any(
                "Homing wird gestartet" in script
                for script in console_scripts))
            self.assertEqual(5, sum(
                "wird angefahren" in script and "Messpunkt" in script
                for script in console_scripts))
            self.assertTrue(any(
                "erfolgreich abgeschlossen" in script
                for script in console_scripts))
            self.assertTrue(result["ok"])
            self.assertTrue(
                store.public()["cameraCalibrationConfigured"])


if __name__ == "__main__":
    unittest.main()
