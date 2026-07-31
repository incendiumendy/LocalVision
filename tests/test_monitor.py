import unittest
from unittest import mock

from local_vision.monitor import (
    VisionMonitor, decide_reaction, validate_policy)


POLICY = {
    "reaction": "cancel-early-pause",
    "early_cancel_minutes": 20,
    "snapshot_interval_seconds": 5,
    "confidence_percent": 85,
    "consecutive_detections": 3,
}


def detection(probability=0.9, usable=True):
    return {
        "failure_probability": probability,
        "image_usable": usable,
    }


class VisionMonitorPolicyTest(unittest.TestCase):
    def test_early_failure_cancels_after_consecutive_gate(self):
        result = decide_reaction(
            POLICY, [detection(), detection(), detection()], 10 * 60)
        self.assertEqual("cancel", result["action"])

    def test_late_failure_pauses_instead_of_canceling(self):
        result = decide_reaction(
            POLICY, [detection(), detection(), detection()], 30 * 60)
        self.assertEqual("pause", result["action"])

    def test_low_confidence_or_unusable_image_never_acts(self):
        low = decide_reaction(
            POLICY, [detection(), detection(0.5), detection()], 60)
        unusable = decide_reaction(
            POLICY, [detection(), detection(usable=False), detection()], 60)
        self.assertEqual("none", low["action"])
        self.assertEqual("none", unusable["action"])

    def test_warning_mode_never_returns_printer_command(self):
        policy = {**POLICY, "reaction": "warn"}
        result = decide_reaction(
            policy, [detection(), detection(), detection()], 60)
        self.assertEqual("warn", result["action"])

    def test_policy_bounds_are_enforced(self):
        with self.assertRaises(ValueError):
            validate_policy({**POLICY, "consecutive_detections": 1})

    def test_printer_actions_are_off_without_explicit_interlock(self):
        monitor = VisionMonitor(
            "http://moonraker", "http://printer", "http://llm/v1",
            "vision-model", POLICY)
        self.assertFalse(monitor._apply_action("pause"))
        self.assertFalse(monitor._apply_action("cancel"))

    def test_confirmed_failure_notifies_home_assistant_once_per_cooldown(self):
        monitor = VisionMonitor(
            "http://moonraker", "http://printer", "http://llm/v1",
            "vision-model", POLICY,
            home_assistant_webhook_url=(
                "http://127.0.0.1:8123/api/webhook/test-id"),
            home_assistant_cooldown_seconds=900)
        event = {
            "created_utc": "2026-07-28T20:00:00Z",
            "telemetry": {"filename": "test.gcode"},
            "observation": {
                "failure_probability": 0.93,
                "failure_type": "spaghetti",
                "visible_evidence": ["loose strands"],
            },
            "decision": {
                "action": "warn",
                "reason": "consecutive_failure_detections",
            },
            "printer_action_executed": False,
        }
        with mock.patch(
                "local_vision.monitor.send_home_assistant_webhook",
                return_value={"delivered": True, "status": 200}) as send:
            first = monitor._notify_home_assistant(event)
            second = monitor._notify_home_assistant(event)
        self.assertEqual("delivered", first["status"])
        self.assertEqual("suppressed", second["status"])
        send.assert_called_once()
        payload = send.call_args.args[1]
        self.assertEqual("print_failure", payload["event_type"])
        self.assertEqual("test.gcode", payload["filename"])


if __name__ == "__main__":
    unittest.main()
