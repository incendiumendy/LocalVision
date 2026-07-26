import unittest

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


if __name__ == "__main__":
    unittest.main()
