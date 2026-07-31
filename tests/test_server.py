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
    ConfigurationError,
    ConfigStore,
    DEFAULT_CONFIG,
    DatasetReader,
    GuidedCalibrationManager,
    SpaghettiTestManager,
    ToolheadNotVisibleError,
    _analyze_spaghetti_frame,
    _calibration_llm_config,
    _model_server_url,
    _png_data_url,
    _require_cold_idle_printer,
    _vision_color_matches,
    aggregate_toolhead_locations,
    ensure_model_loaded,
    image_dimensions,
    locate_moved_toolhead,
    locate_toolhead_in_frame,
    mainsail_theme,
    make_handler,
    openai_url,
    parse_gcode_layers,
    render_layer_reference,
    toolhead_bbox_has_camera_margin,
    unload_model_if_supported,
    validate_base_url,
    warmup_vision_model,
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

    def test_calibration_model_lifecycle_uses_server_root(self):
        config = dict(DEFAULT_CONFIG)
        config.update({
            "base_url": "http://127.0.0.1:8091/v1",
            "model": "/models/vision.gguf",
            "timeout_seconds": 45,
        })
        self.assertEqual(
            "http://127.0.0.1:8091/models/load",
            _model_server_url(config, "models/load"))
        self.assertEqual(
            180, _calibration_llm_config(config)["timeout_seconds"])
        with mock.patch(
                "local_vision.server._llama_router_model_status",
                side_effect=[
                    {"value": "unloaded"},
                    {"value": "loading"},
                    {"value": "loaded"},
                ]), mock.patch(
                    "local_vision.server._json_request") as request, mock.patch(
                    "local_vision.server.time.sleep"):
            loaded = ensure_model_loaded(config)
        self.assertTrue(loaded["loadRequested"])
        self.assertEqual(
            "http://127.0.0.1:8091/models/load",
            request.call_args.args[0])

    def test_fixed_model_warmup_uses_180_seconds_and_idle_unload(self):
        config = dict(DEFAULT_CONFIG)
        config.update({
            "base_url": "http://127.0.0.1:8091/v1",
            "model": "/models/vision.gguf",
            "timeout_seconds": 45,
        })
        with mock.patch(
                "local_vision.server._llama_router_model_status",
                return_value=None):
            self.assertEqual(
                "fixed-or-idle", ensure_model_loaded(config)["manager"])
            self.assertFalse(unload_model_if_supported(config))
        with mock.patch(
                "local_vision.server._chat",
                return_value=("VISION_READY", 48200)) as chat:
            warmed = warmup_vision_model(config)
        self.assertEqual(48200, warmed["latencyMs"])
        self.assertEqual(180, chat.call_args.args[0]["timeout_seconds"])
        content = chat.call_args.args[1][0]["content"]
        self.assertEqual("image_url", content[1]["type"])

    def test_router_model_is_unloaded_immediately(self):
        config = dict(DEFAULT_CONFIG)
        config.update({
            "base_url": "http://127.0.0.1:8091/v1",
            "model": "/models/vision.gguf",
        })
        with mock.patch(
                "local_vision.server._llama_router_model_status",
                return_value={"value": "loaded"}), mock.patch(
                    "local_vision.server._json_request") as request:
            self.assertTrue(unload_model_if_supported(config))
        self.assertEqual(
            "http://127.0.0.1:8091/models/unload",
            request.call_args.args[0])

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

    def test_moved_toolhead_requires_and_returns_before_after_coordinates(self):
        answer = json.dumps({
            "before": {"x": 0.5, "y": 0.5},
            "after": {"x": 0.2, "y": 0.25},
            "confidence": 0.91,
            "visible_before": True,
            "visible_after": True,
            "target": "nozzle",
        })
        with mock.patch(
                "local_vision.server._chat",
                return_value=(answer, 17)):
            result = locate_moved_toolhead(
                dict(DEFAULT_CONFIG),
                b"before",
                "image/jpeg",
                b"after",
                "image/jpeg")
        self.assertEqual(0.5, result["beforeX"])
        self.assertEqual(0.2, result["x"])
        self.assertGreater(result["motion"], 0.02)
        self.assertEqual(answer, result["rawAnswer"])

    def test_single_frame_toolhead_uses_bbox_center(self):
        answer = json.dumps({
            "bbox": [181, 0, 481, 403],
            "confidence": 0.999,
            "visible": True,
            "target": "toolhead",
        })
        with mock.patch(
                "local_vision.server._chat",
                return_value=(answer, 19)):
            result = locate_toolhead_in_frame(
                dict(DEFAULT_CONFIG),
                b"frame",
                "image/jpeg")
        self.assertAlmostEqual(0.331, result["x"])
        self.assertAlmostEqual(0.2015, result["y"])
        self.assertEqual([0.181, 0.0, 0.481, 0.403], result["bbox"])
        self.assertEqual("toolhead", result["target"])
        self.assertEqual(answer, result["rawAnswer"])

    def test_toolhead_bbox_requires_camera_margin(self):
        self.assertTrue(toolhead_bbox_has_camera_margin({
            "bbox": [0.05, 0.05, 0.8, 0.8],
        }))
        self.assertFalse(toolhead_bbox_has_camera_margin({
            "bbox": [0.0, 0.05, 0.8, 0.8],
        }))

    def test_camera_plan_moves_invisible_rear_toward_visible_front(self):
        plan = {
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
        visible = {
            "bbox": [0.1, 0.1, 0.3, 0.3],
            "x": 0.2,
            "y": 0.2,
        }
        invisible = ToolheadNotVisibleError(
            "not visible", '{"visible":false}')
        with ExitStack() as stack:
            stack.enter_context(mock.patch(
                "local_vision.server._require_idle_printer"))
            command = stack.enter_context(mock.patch(
                "local_vision.server._moonraker_command"))
            stack.enter_context(mock.patch(
                "local_vision.server.camera_snapshot",
                return_value=(
                    {"uid": "cam-1", "name": "cam1"},
                    b"image",
                    "image/jpeg")))
            stack.enter_context(mock.patch(
                "local_vision.server.image_dimensions",
                return_value=(960, 720)))
            locator = stack.enter_context(mock.patch(
                "local_vision.server.locate_toolhead_by_color",
                side_effect=[
                    visible,
                    visible,
                    invisible,
                    invisible,
                    invisible,
                    invisible,
                    invisible,
                    visible,
                    visible,
                    visible,
                ]))
            fitted, starting_location = (
                GuidedCalibrationManager._fit_plan_to_camera(
                    dict(DEFAULT_CONFIG),
                    plan,
                    {"uid": "cam-1", "name": "cam1"},
                    {"hue": 60},
                    (960, 720),
                    False))
        self.assertEqual(159.0, fitted["points"][2]["x"])
        self.assertEqual(78.0, fitted["points"][2]["y"])
        self.assertIs(visible, starting_location)
        self.assertEqual(10, locator.call_count)
        self.assertEqual(10, command.call_count)

    def test_repeated_locations_are_medianed_and_unstable_data_is_rejected(self):
        stable = [{
            "beforeX": 0.5 + offset,
            "beforeY": 0.5,
            "x": 0.2 + offset,
            "y": 0.25,
            "confidence": 0.9,
            "target": "nozzle",
            "latencyMs": 10,
        } for offset in (-0.002, 0.0, 0.002)]
        result = aggregate_toolhead_locations(stable)
        self.assertAlmostEqual(0.2, result["x"])
        self.assertEqual(3, result["sampleCount"])
        unstable = [dict(item) for item in stable]
        unstable[2]["x"] = 0.5
        with self.assertRaisesRegex(RuntimeError, "streut zu stark"):
            aggregate_toolhead_locations(unstable)

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
                with urllib.request.urlopen(
                        base + "/api/spaghetti/status") as response:
                    spaghetti = json.load(response)
                self.assertEqual("idle", spaghetti["state"])
                self.assertFalse(spaghetti["motionCommands"])
                self.assertFalse(spaghetti["heaterCommands"])
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

    def test_spaghetti_prepare_is_cold_motionless_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "config.json")
            config_path.write_text(
                json.dumps(dict(DEFAULT_CONFIG)), encoding="utf-8")
            manager = SpaghettiTestManager(ConfigStore(config_path))
            camera = {"uid": "cam-1", "name": "cam1"}
            safety = {
                "printState": "standby",
                "extruderTemperature": 24.0,
                "bedTemperature": 23.0,
                "heaterTargetsZero": True,
                "liveVelocity": 0.0,
            }
            with mock.patch(
                    "local_vision.server._require_cold_idle_printer",
                    return_value=safety), mock.patch(
                    "local_vision.server.camera_snapshot",
                    return_value=(camera, b"clean-frame", "image/jpeg")
                    ), mock.patch(
                    "local_vision.server.image_dimensions",
                    return_value=(960, 720)), mock.patch(
                    "local_vision.server._moonraker_command"
                    ) as printer_command, mock.patch(
                    "local_vision.server.ensure_model_loaded"
                    ) as model_load:
                prepared = manager.prepare({
                    "confirmation": "COLD_IDLE_REFERENCE",
                })
            self.assertEqual("awaiting_spaghetti", prepared["state"])
            self.assertFalse(prepared["motionCommands"])
            self.assertFalse(prepared["heaterCommands"])
            printer_command.assert_not_called()
            model_load.assert_not_called()
            runs = list(Path(directory, "spaghetti-tests").iterdir())
            self.assertEqual(1, len(runs))
            self.assertEqual(
                b"clean-frame",
                Path(runs[0], "reference.jpg").read_bytes())
            metadata = json.loads(
                Path(runs[0], "metadata.json").read_text(encoding="utf-8"))
            self.assertFalse(metadata["motion_commands"])
            self.assertFalse(metadata["heater_commands"])
            manager.cancel({"sessionToken": prepared["sessionToken"]})

    def test_spaghetti_failed_prepare_releases_session_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "config.json")
            config_path.write_text(
                json.dumps(dict(DEFAULT_CONFIG)), encoding="utf-8")
            manager = SpaghettiTestManager(ConfigStore(config_path))
            safety = {
                "printState": "standby",
                "extruderTemperature": 24.0,
                "bedTemperature": 23.0,
                "heaterTargetsZero": True,
                "liveVelocity": 0.0,
            }
            with mock.patch(
                    "local_vision.server._require_cold_idle_printer",
                    return_value=safety), mock.patch(
                    "local_vision.server.camera_snapshot",
                    side_effect=RuntimeError("Kamera offline")):
                with self.assertRaises(RuntimeError):
                    manager.prepare({
                        "confirmation": "COLD_IDLE_REFERENCE",
                    })
            self.assertEqual("idle", manager.status()["state"])
            self.assertEqual(
                [],
                list(Path(directory).glob("spaghetti-tests/*")))
            camera = {"uid": "cam-1", "name": "cam1"}
            with mock.patch(
                    "local_vision.server._require_cold_idle_printer",
                    return_value=safety), mock.patch(
                    "local_vision.server.camera_snapshot",
                    return_value=(camera, b"clean-frame", "image/jpeg")
                    ), mock.patch(
                    "local_vision.server.image_dimensions",
                    return_value=(960, 720)):
                prepared = manager.prepare({
                    "confirmation": "COLD_IDLE_REFERENCE",
                })
            self.assertEqual("awaiting_spaghetti", prepared["state"])
            manager.cancel({"sessionToken": prepared["sessionToken"]})

    def test_spaghetti_status_reports_preparing_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SpaghettiTestManager(
                ConfigStore(Path(directory, "config.json")))
            manager.session = {"token": "reserved", "state": "preparing"}
            status = manager.status()
            self.assertEqual("preparing", status["state"])
            self.assertFalse(status["motionCommands"])
            self.assertFalse(status["heaterCommands"])
            self.assertNotIn("sessionToken", status)

    def test_mainsail_theme_reads_live_ratos_primary(self):
        config = dict(DEFAULT_CONFIG)
        with mock.patch(
                "local_vision.server._moonraker_json",
                return_value={"value": "#2196F3"}) as moonraker:
            theme = mainsail_theme(config)
        self.assertEqual("#2196F3", theme["primary"])
        self.assertEqual("mainsail", theme["source"])
        self.assertIn(
            "namespace=mainsail&key=uiSettings.primary",
            moonraker.call_args[0][1])
        with mock.patch(
                "local_vision.server._moonraker_json",
                return_value={"value": None}):
            self.assertIsNone(mainsail_theme(config)["primary"])
        with mock.patch(
                "local_vision.server._moonraker_json",
                return_value={"value": "lime"}):
            self.assertIsNone(mainsail_theme(config)["primary"])
        with mock.patch(
                "local_vision.server._moonraker_json",
                side_effect=RuntimeError("offline")):
            theme = mainsail_theme(config)
        self.assertIsNone(theme["primary"])
        self.assertEqual("unavailable", theme["source"])

    def test_spaghetti_classifier_accepts_binary_model_answer(self):
        difference = {
            "changedPixelRatio": 0.0173,
            "largestChangeBox": [0.45, 0.41, 0.59, 0.50],
        }
        with mock.patch(
                "local_vision.server._chat",
                return_value=("SPAGHETTI", 1200)) as chat:
            result = _analyze_spaghetti_frame(
                dict(DEFAULT_CONFIG), b"image", "image/jpeg", difference)
        self.assertTrue(result["spaghettiDetected"])
        self.assertEqual(0.95, result["confidence"])
        self.assertEqual(
            "binary-model-plus-reference-difference",
            result["confidenceSource"])
        self.assertEqual(20, chat.call_args.kwargs["max_tokens"])

    def test_spaghetti_analysis_uses_reference_without_printer_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory, "config.json")
            config = dict(DEFAULT_CONFIG)
            config["model"] = "vision-model"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            manager = SpaghettiTestManager(ConfigStore(config_path))
            camera = {"uid": "cam-1", "name": "cam1"}
            safety = {
                "printState": "standby",
                "extruderTemperature": 24.0,
                "bedTemperature": 23.0,
                "heaterTargetsZero": True,
                "liveVelocity": 0.0,
            }
            with ExitStack() as stack:
                stack.enter_context(mock.patch(
                    "local_vision.server._require_cold_idle_printer",
                    return_value=safety))
                stack.enter_context(mock.patch(
                    "local_vision.server.camera_snapshot",
                    side_effect=[
                        (camera, b"clean-frame", "image/jpeg"),
                        (camera, b"spaghetti-frame", "image/jpeg"),
                    ]))
                stack.enter_context(mock.patch(
                    "local_vision.server.image_dimensions",
                    return_value=(960, 720)))
                stack.enter_context(mock.patch(
                    "local_vision.server._spaghetti_image_difference",
                    return_value={
                        "changedPixelRatio": 0.02,
                        "changedPixels": 13824,
                        "largestChangeBox": [0.2, 0.2, 0.6, 0.7],
                        "imageSize": [960, 720],
                    }))
                stack.enter_context(mock.patch(
                    "local_vision.server.ensure_model_loaded"))
                stack.enter_context(mock.patch(
                    "local_vision.server.warmup_vision_model",
                    return_value={"latencyMs": 50}))
                stack.enter_context(mock.patch(
                    "local_vision.server._analyze_spaghetti_frame",
                    return_value={
                        "spaghettiDetected": True,
                        "confidence": 0.91,
                        "description": "lose Filamentfäden",
                        "x": 0.4,
                        "y": 0.45,
                        "latencyMs": 100,
                        "rawAnswer": "{}",
                    }))
                stack.enter_context(mock.patch(
                    "local_vision.server.unload_model_if_supported",
                    return_value=False))
                printer_command = stack.enter_context(mock.patch(
                    "local_vision.server._moonraker_command"))
                prepared = manager.prepare({
                    "confirmation": "COLD_IDLE_REFERENCE",
                })
                result = manager.analyze({
                    "sessionToken": prepared["sessionToken"],
                })
            self.assertTrue(result["spaghettiDetected"])
            self.assertEqual(0.91, result["confidence"])
            self.assertFalse(result["motionCommands"])
            self.assertFalse(result["heaterCommands"])
            printer_command.assert_not_called()
            self.assertEqual("idle", manager.status()["state"])
            runs = list(Path(directory, "spaghetti-tests").iterdir())
            self.assertTrue(Path(runs[0], "result.json").is_file())

    def test_spaghetti_safety_rejects_heating_or_motion(self):
        base_state = {
            "webhooks": {"state": "ready"},
            "print_stats": {"state": "standby"},
            "extruder": {"temperature": 24.0, "target": 0.0},
            "heater_bed": {"temperature": 23.0, "target": 0.0},
            "motion_report": {"live_velocity": 0.0},
        }
        with mock.patch(
                "local_vision.server._moonraker_json",
                return_value={"status": {
                    **base_state,
                    "extruder": {"temperature": 24.0, "target": 200.0},
                }}):
            with self.assertRaises(ConfigurationError):
                _require_cold_idle_printer(dict(DEFAULT_CONFIG))
        with mock.patch(
                "local_vision.server._moonraker_json",
                return_value={"status": {
                    **base_state,
                    "motion_report": {"live_velocity": 25.0},
                }}):
            with self.assertRaises(ConfigurationError):
                _require_cold_idle_printer(dict(DEFAULT_CONFIG))

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
        styles = Path(
            project,
            "web",
            "styles.css",
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
        self.assertIn("app.js?v=10", html)
        self.assertIn("styles.css?v=9", html)
        self.assertIn("in der Mainsail-Konsole", html)
        self.assertIn("Pro Messpunkt werden drei", html)
        self.assertIn("Diagnosedaten gespeichert", html)
        self.assertIn('"/api/camera/calibration/run"', script)
        self.assertIn("Kontrollierter Spaghetti-Test", html)
        self.assertEqual(1, html.count('id="spaghetti-prepare"'))
        self.assertEqual(1, html.count('id="spaghetti-analyze"'))
        self.assertEqual(1, html.count('id="spaghetti-cancel"'))
        self.assertIn('"/api/spaghetti/status"', script)
        self.assertIn('"/api/spaghetti/prepare"', script)
        self.assertIn('"/api/spaghetti/analyze"', script)
        self.assertIn('"/api/spaghetti/cancel"', script)
        self.assertIn("COLD_IDLE_REFERENCE", script)
        self.assertLess(
            html.index("Kontrollierter Spaghetti-Test"),
            html.index("Home Assistant Alarm"))
        self.assertIn('"/api/theme"', script)
        self.assertIn("applyThemeColor", script)
        self.assertIn("--primary-rgb", styles)
        self.assertIn("--primary-ink", styles)
        self.assertIn("#99f321", styles)

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
            detected = [{
                "x": 0.5,
                "y": 0.5,
                "bbox": [0.4, 0.4, 0.6, 0.6],
                "confidence": 0.95,
                "target": "toolhead",
                "latencyMs": 10,
                "rawAnswer": "{}",
            }]
            for point in locations:
                for offset in (-0.002, 0.0, 0.002):
                    detected.append({
                        "x": point["x"] + offset,
                        "y": point["y"],
                        "bbox": [
                            point["x"] - 0.1 + offset,
                            point["y"] - 0.1,
                            point["x"] + 0.1 + offset,
                            point["y"] + 0.1,
                        ],
                        "confidence": 0.95,
                        "target": "toolhead",
                        "latencyMs": 10,
                        "rawAnswer": "{}",
                    })
            camera = {"uid": "cam-1", "name": "cam1"}
            with ExitStack() as stack:
                stack.enter_context(mock.patch(
                    "local_vision.server.ensure_model_loaded",
                    return_value={
                        "manager": "fixed-or-idle",
                        "loadRequested": False,
                    }))
                stack.enter_context(mock.patch(
                    "local_vision.server.warmup_vision_model",
                    return_value={
                        "ok": True,
                        "latencyMs": 100,
                        "timeoutSeconds": 180,
                    }))
                stack.enter_context(mock.patch(
                    "local_vision.server.unload_model_if_supported",
                    return_value=False))
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
                    "local_vision.server.image_dimensions",
                    return_value=(960, 720)))
                stack.enter_context(mock.patch(
                    "local_vision.server.learn_toolhead_color_tracker",
                    return_value={
                        "method": "learned-hue",
                        "hue": 60,
                    }))
                stack.enter_context(mock.patch(
                    "local_vision.server.GuidedCalibrationManager."
                    "_fit_plan_to_camera",
                    return_value=(plan, detected[0])))
                initial_locator = stack.enter_context(mock.patch(
                    "local_vision.server.locate_toolhead_in_frame",
                    return_value=detected[0]))
                locator = stack.enter_context(mock.patch(
                    "local_vision.server.locate_toolhead_by_color",
                    side_effect=detected[1:]))
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
            self.assertTrue(any(
                "Analyse 3/3" in script
                for script in console_scripts))
            self.assertTrue(any(
                "Bildposition" in script
                for script in console_scripts))
            self.assertEqual(15, locator.call_count)
            self.assertEqual(1, initial_locator.call_count)
            self.assertEqual(
                180,
                initial_locator.call_args_list[0].args[0]["timeout_seconds"])
            self.assertTrue(result["ok"])
            self.assertTrue(
                store.public()["cameraCalibrationConfigured"])
            run_dir = Path(
                directory,
                "calibration-runs",
                result["diagnosticRun"])
            metadata = json.loads(
                Path(run_dir, "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual("calibrated", metadata["state"])
            self.assertEqual(5, len(metadata["points"]))
            self.assertEqual(3, len(metadata["points"][0]["analyses"]))
            self.assertEqual(
                16,
                len(list(run_dir.glob("*.jpg"))))

    def test_guided_calibration_rejects_duplicate_points_with_diagnostics(self):
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
            detected = [{
                "x": 0.5,
                "y": 0.5,
                "bbox": [0.4, 0.4, 0.6, 0.6],
                "confidence": 0.9,
                "target": "toolhead",
                "latencyMs": 10,
                "rawAnswer": "{}",
            }]
            for x_position in (0.2, 0.223):
                for offset in (-0.001, 0.0, 0.001):
                    detected.append({
                        "x": x_position + offset,
                        "y": 0.2,
                        "bbox": [
                            x_position - 0.1 + offset,
                            0.1,
                            x_position + 0.1 + offset,
                            0.3,
                        ],
                        "confidence": 0.9,
                        "target": "toolhead",
                        "latencyMs": 10,
                        "rawAnswer": "{}",
                    })
            camera = {"uid": "cam-1", "name": "cam1"}
            with ExitStack() as stack:
                stack.enter_context(mock.patch(
                    "local_vision.server.ensure_model_loaded",
                    return_value={
                        "manager": "fixed-or-idle",
                        "loadRequested": False,
                    }))
                stack.enter_context(mock.patch(
                    "local_vision.server.warmup_vision_model",
                    return_value={
                        "ok": True,
                        "latencyMs": 100,
                        "timeoutSeconds": 180,
                    }))
                stack.enter_context(mock.patch(
                    "local_vision.server.unload_model_if_supported",
                    return_value=False))
                stack.enter_context(mock.patch(
                    "local_vision.server._selected_camera",
                    return_value=camera))
                stack.enter_context(mock.patch(
                    "local_vision.server._require_idle_printer",
                    return_value=(state, plan)))
                stack.enter_context(mock.patch(
                    "local_vision.server._klipper_respond_available",
                    return_value=False))
                stack.enter_context(mock.patch(
                    "local_vision.server._moonraker_command"))
                stack.enter_context(mock.patch(
                    "local_vision.server.camera_snapshot",
                    return_value=(camera, b"image", "image/jpeg")))
                stack.enter_context(mock.patch(
                    "local_vision.server.image_dimensions",
                    return_value=(960, 720)))
                stack.enter_context(mock.patch(
                    "local_vision.server.learn_toolhead_color_tracker",
                    return_value={
                        "method": "learned-hue",
                        "hue": 60,
                    }))
                stack.enter_context(mock.patch(
                    "local_vision.server.GuidedCalibrationManager."
                    "_fit_plan_to_camera",
                    return_value=(plan, detected[0])))
                stack.enter_context(mock.patch(
                    "local_vision.server.locate_toolhead_in_frame",
                    return_value=detected[0]))
                stack.enter_context(mock.patch(
                    "local_vision.server.locate_toolhead_by_color",
                    side_effect=detected[1:]))
                prepared = manager.prepare({
                    "motionConfirmation": "HOME_AND_MOVE",
                })
                with self.assertRaisesRegex(RuntimeError, "nicht eindeutig"):
                    manager.run({
                        "sessionToken": prepared["sessionToken"],
                        "motionConfirmation": "HOME_AND_MOVE",
                    })
            self.assertFalse(
                store.public()["cameraCalibrationConfigured"])
            runs = list(Path(directory, "calibration-runs").iterdir())
            self.assertEqual(1, len(runs))
            metadata = json.loads(
                Path(runs[0], "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", metadata["state"])
            self.assertIn("nicht eindeutig", metadata["error"])


if __name__ == "__main__":
    unittest.main()
    ensure_model_loaded,
