import unittest

from local_vision.calibration import (
    CalibrationError,
    build_calibration_plan,
    project_point,
    solve_homography,
    validate_center_point,
)


class CameraCalibrationGeometryTest(unittest.TestCase):
    def test_plan_uses_conservative_points_inside_live_limits(self):
        plan = build_calibration_plan(
            [0, 0, -5, 0], [300, 300, 300, 0])
        self.assertEqual(20.0, plan["safe_z"])
        self.assertEqual(
            [
                ("front-left", 60.0, 60.0),
                ("front-right", 240.0, 60.0),
                ("rear-right", 240.0, 240.0),
                ("rear-left", 60.0, 240.0),
                ("center", 150.0, 150.0),
            ],
            [
                (point["name"], point["x"], point["y"])
                for point in plan["points"]
            ])

    def test_plan_rejects_too_small_or_invalid_motion_space(self):
        with self.assertRaises(CalibrationError):
            build_calibration_plan([0, 0, 0], [80, 300, 300])
        with self.assertRaises(CalibrationError):
            build_calibration_plan([0, 0], [300, 300])

    def test_homography_maps_bed_corners_and_validates_center(self):
        bed = [(60, 60), (240, 60), (240, 240), (60, 240)]
        image = [(0.2, 0.7), (0.8, 0.65), (0.75, 0.2), (0.25, 0.25)]
        homography = solve_homography(bed, image)
        for expected, point in zip(image, bed):
            actual = project_point(homography, point[0], point[1])
            self.assertAlmostEqual(expected[0], actual[0], places=7)
            self.assertAlmostEqual(expected[1], actual[1], places=7)
        center = project_point(homography, 150, 150)
        self.assertLess(
            validate_center_point(homography, (150, 150), center),
            1e-9)


if __name__ == "__main__":
    unittest.main()
