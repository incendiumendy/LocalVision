import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from local_vision.notifications import (
    NotificationConfigurationError,
    send_home_assistant_webhook,
    validate_home_assistant_webhook_url,
)


class _WebhookHandler(BaseHTTPRequestHandler):
    event = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).event = json.loads(
            self.rfile.read(length).decode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, message, *args):
        pass


class HomeAssistantNotificationTest(unittest.TestCase):
    def test_rejects_public_or_non_webhook_destination(self):
        with mock.patch(
                "local_vision.notifications.socket.getaddrinfo",
                return_value=[
                    (None, None, None, None, ("8.8.8.8", 8123)),
                ]):
            with self.assertRaises(NotificationConfigurationError):
                validate_home_assistant_webhook_url(
                    "http://example.com:8123/api/webhook/id")
        with self.assertRaises(NotificationConfigurationError):
            validate_home_assistant_webhook_url(
                "http://127.0.0.1:8123/api/services/notify")

    def test_posts_json_event_to_private_webhook(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _WebhookHandler)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/api/webhook/test-id" % (
                server.server_port)
            result = send_home_assistant_webhook(
                url,
                {
                    "event_type": "test",
                    "title": "Local Vision Testalarm",
                })
            self.assertTrue(result["delivered"])
            self.assertEqual(200, result["status"])
            self.assertEqual(
                "test", _WebhookHandler.event["event_type"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
