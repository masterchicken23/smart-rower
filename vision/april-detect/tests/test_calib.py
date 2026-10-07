"""Loading calibrations and adapting them to the streamed resolution."""

from __future__ import annotations

import json

import numpy as np
import pytest

from april_detect import calib

ROS_YAML = """
image_width: 1920
image_height: 1080
camera_name: cam1
camera_matrix: {rows: 3, cols: 3, data: [1400, 0, 959.5, 0, 1400, 539.5, 0, 0, 1]}
distortion_model: plumb_bob
distortion_coefficients: {rows: 1, cols: 5, data: [-0.2, 0.05, 0, 0, 0]}
"""


def test_ros_yaml(tmp_path):
    p = tmp_path / "cam1.yaml"
    p.write_text(ROS_YAML)
    c = calib.from_file(str(p))
    assert c.size == (1920, 1080) and c.model == "pinhole"
    assert c.K[0, 0] == 1400 and c.D[0] == -0.2


def test_json_same_layout(tmp_path):
    p = tmp_path / "cam1.json"
    p.write_text(json.dumps({"image_width": 640, "image_height": 480,
                             "camera_matrix": [500, 0, 320, 0, 500, 240, 0, 0, 1],
                             "distortion_model": "equidistant",
                             "distortion_coefficients": [0.1, 0, 0, 0]}))
    c = calib.from_file(str(p))
    assert c.model == "fisheye" and c.size == (640, 480)


def test_npz_like_the_experiment(tmp_path):
    p = tmp_path / "c.npz"
    np.savez(p, K=np.array([[600, 0, 320], [0, 600, 240], [0, 0, 1.0]]),
             dist=np.array([0.1, 0, 0, 0, 0]), image_size=np.array([640, 480]))
    c = calib.from_file(str(p))
    assert c.size == (640, 480) and c.D[0] == 0.1


def test_inline_four_values():
    c = calib.from_inline("900, 900, 639.5, 359.5", "-0.1 0.01 0 0", "plumb_bob",
                          "1280x720")
    assert c.K[0, 2] == 639.5 and c.size == (1280, 720) and c.D.size == 4


def test_inline_rejects_wrong_count():
    with pytest.raises(ValueError):
        calib.from_inline("1 2 3", "", "pinhole", "")


def test_scaling_keeps_pixel_centres():
    c = calib.Calibration(calib.K_from(1400, 1400, 959.5, 539.5), np.zeros(5),
                          size=(1920, 1080))
    s = c.scaled_to(1280, 720)
    # the optical centre of a centred camera stays centred
    assert abs(s.K[0, 2] - 639.5) < 1e-9 and abs(s.K[1, 2] - 359.5) < 1e-9
    assert abs(s.K[0, 0] - 1400 * 2 / 3) < 1e-9


def test_different_aspect_is_refused():
    c = calib.Calibration(calib.K_from(500, 500, 320, 240), np.zeros(0),
                          size=(640, 480))
    with pytest.raises(ValueError):
        c.scaled_to(1280, 720)


def test_camera_model_falls_back_on_aspect_mismatch():
    c = calib.Calibration(calib.K_from(500, 500, 320, 240), np.zeros(0),
                          size=(640, 480))
    u = calib.CameraModel(c, "image", -1, 70.0).for_size(1280, 720)
    assert not u.calib.calibrated


def test_no_distortion_means_no_remap():
    c = calib.Calibration(calib.K_from(500, 500, 320, 240), np.zeros(5),
                          size=(640, 480))
    u = calib.Undistorter(c, "image")
    assert u.mode == "none" and u.maps is None
