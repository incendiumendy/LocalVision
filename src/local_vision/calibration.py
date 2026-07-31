"""Geometry helpers for guided camera calibration."""

import math


class CalibrationError(ValueError):
    """Raised when a safe or geometrically valid calibration is impossible."""


def build_calibration_plan(axis_minimum, axis_maximum):
    """Build five conservative in-bounds points from live Klipper limits."""
    if len(axis_minimum) < 3 or len(axis_maximum) < 3:
        raise CalibrationError("Klipper liefert keine vollständigen Achsgrenzen.")
    values = [float(value) for value in axis_minimum[:3] + axis_maximum[:3]]
    if not all(math.isfinite(value) for value in values):
        raise CalibrationError("Die Achsgrenzen enthalten ungültige Werte.")
    x_min, y_min, z_min, x_max, y_max, z_max = values
    width = x_max - x_min
    depth = y_max - y_min
    z_range = z_max - z_min
    if width < 100 or depth < 100 or z_range < 30:
        raise CalibrationError(
            "Der konfigurierte Bewegungsraum ist für die Kalibrierung zu klein.")
    x_margin = max(30.0, width * 0.2)
    y_margin = max(30.0, depth * 0.2)
    if x_margin * 2 >= width or y_margin * 2 >= depth:
        raise CalibrationError("Es bleibt kein sicherer Kalibrierbereich.")
    x_low = x_min + x_margin
    x_high = x_max - x_margin
    y_low = y_min + y_margin
    y_high = y_max - y_margin
    center_x = (x_min + x_max) / 2.0
    center_y = (y_min + y_max) / 2.0
    safe_z = min(z_max - 10.0, max(z_min + 20.0, 20.0))
    if safe_z <= z_min or safe_z >= z_max:
        raise CalibrationError("Es konnte keine sichere Z-Höhe bestimmt werden.")
    return {
        "axis_minimum": [x_min, y_min, z_min],
        "axis_maximum": [x_max, y_max, z_max],
        "bed_width": width,
        "bed_depth": depth,
        "safe_z": safe_z,
        "travel_speed_mm_s": 50.0,
        "points": [
            {"name": "front-left", "x": x_low, "y": y_low},
            {"name": "front-right", "x": x_high, "y": y_low},
            {"name": "rear-right", "x": x_high, "y": y_high},
            {"name": "rear-left", "x": x_low, "y": y_high},
            {"name": "center", "x": center_x, "y": center_y},
        ],
    }


def _solve_linear(matrix, vector):
    size = len(vector)
    rows = [
        [float(value) for value in matrix[index]] + [float(vector[index])]
        for index in range(size)
    ]
    for column in range(size):
        pivot = max(
            range(column, size),
            key=lambda row: abs(rows[row][column]))
        if abs(rows[pivot][column]) < 1e-10:
            raise CalibrationError("Die Kalibrierpunkte sind nicht eindeutig.")
        rows[column], rows[pivot] = rows[pivot], rows[column]
        scale = rows[column][column]
        rows[column] = [value / scale for value in rows[column]]
        for row in range(size):
            if row == column:
                continue
            factor = rows[row][column]
            rows[row] = [
                rows[row][index] - factor * rows[column][index]
                for index in range(size + 1)
            ]
    return [rows[index][-1] for index in range(size)]


def solve_homography(bed_points, image_points):
    """Return a bed-coordinate to normalized-image homography."""
    if len(bed_points) != 4 or len(image_points) != 4:
        raise CalibrationError("Für die Projektion werden vier Punkte benötigt.")
    matrix = []
    vector = []
    for (x_pos, y_pos), (u_pos, v_pos) in zip(
            bed_points, image_points):
        values = [x_pos, y_pos, 1.0]
        matrix.append(values + [0.0, 0.0, 0.0]
                      + [-u_pos * x_pos, -u_pos * y_pos])
        vector.append(u_pos)
        matrix.append([0.0, 0.0, 0.0] + values
                      + [-v_pos * x_pos, -v_pos * y_pos])
        vector.append(v_pos)
    solved = _solve_linear(matrix, vector)
    homography = [
        solved[0:3],
        solved[3:6],
        solved[6:8] + [1.0],
    ]
    for x_pos, y_pos in bed_points:
        project_point(homography, x_pos, y_pos)
    return homography


def project_point(homography, x_pos, y_pos):
    denominator = (
        homography[2][0] * x_pos
        + homography[2][1] * y_pos
        + homography[2][2]
    )
    if abs(denominator) < 1e-10:
        raise CalibrationError("Die Projektion ist an diesem Punkt ungültig.")
    return (
        (
            homography[0][0] * x_pos
            + homography[0][1] * y_pos
            + homography[0][2]
        ) / denominator,
        (
            homography[1][0] * x_pos
            + homography[1][1] * y_pos
            + homography[1][2]
        ) / denominator,
    )


def validate_center_point(homography, bed_point, image_point):
    """Return normalized reprojection error for the independent fifth point."""
    projected = project_point(homography, bed_point[0], bed_point[1])
    return math.hypot(
        projected[0] - image_point[0],
        projected[1] - image_point[1])
