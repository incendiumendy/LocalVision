import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from local_vision.server import (
    ConfigStore,
    DatasetReader,
    _png_data_url,
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

    def test_public_config_never_returns_api_key(self):
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
                })
            self.assertTrue(public["apiKeyConfigured"])
            self.assertNotIn("api_key", public)
            self.assertNotIn("top-secret", json.dumps(public))

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
                self.assertFalse(payload["printerControl"])
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


if __name__ == "__main__":
    unittest.main()
