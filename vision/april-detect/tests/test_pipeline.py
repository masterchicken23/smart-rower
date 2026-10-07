"""End to end through Pipeline.process(): synthetic JPEG in, record out,
checked against the pose the frame was rendered with.

These are the tests that pin the things easiest to get silently wrong: the
tag-frame convention, that undistortion actually corrects (and that skipping it
measurably does not), resolution scaling of a calibration, and the record's
shape and timing fields.
"""

from __future__ import annotations

import json
import math

import cv2
import numpy as np
import pytest

from april_detect import pose, synth, wire
from april_detect.calib import Calibration, K_from
from april_detect.config import Settings
from april_detect.pipeline import Pipeline

W, H = 1280, 720
K = K_from(700, 700, 639.5, 359.5)
BARREL = np.array([-0.30, 0.09, 0.0, 0.0, 0.0])
FISHEYE = np.array([-0.02, 0.01, 0.0, 0.0])


def settings(**kw) -> Settings:
    base = {"detector": "cpu", "threads": 2, "decimate": 1.0,
            "heartbeat_path": "", "camera_matrix": "700 700 639.5 359.5",
            "calib_size": f"{W}x{H}"}
    base.update(kw)
    return Settings(_env_file=None, **base)


def frame(cal: Calibration, tags: list[dict], header: dict | None = None,
          quality: int = 92) -> list[bytes]:
    img = synth.render(cal, tags)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    msg = header if header is not None else {"cam": "t", "seq": 1}
    return [json.dumps({"msg": msg}).encode(), jpg.tobytes()]


def rot_err_deg(Ra: np.ndarray, Rb: np.ndarray) -> float:
    c = (np.trace(Ra.T @ Rb) - 1) / 2
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def quat_to_R(q) -> np.ndarray:
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def check(rec, truth, pos_tol_m=0.01, rot_tol_deg=2.0):
    assert rec["n"] == len(truth), rec
    by_id = {t["id"]: t for t in rec["tags"]}
    for tr in truth:
        got = by_id[tr["id"]]
        dp = np.linalg.norm(np.array(got["pos_m"]) - np.asarray(tr["t"]))
        dr = rot_err_deg(quat_to_R(got["quat"]), tr["R"])
        assert dp < pos_tol_m, (dp, got["pos_m"], tr["t"])
        assert dr < rot_tol_deg, (dr, got["euler_deg"])


def run(s: Settings, parts: list[bytes]):
    return Pipeline(s).process(parts, 0.0, 1_760_000_000.0)


# ---- convention --------------------------------------------------------- #
def test_upright_tag_facing_camera_is_identity():
    """The convention: an upright tag squarely facing the camera reads as the
    identity rotation. In-plane spin (rz) and position are well conditioned
    face-on and are checked tightly. Tilt is not -- see the next test."""
    cal = Calibration(K, np.zeros(0), size=(W, H))
    tag = {"id": 0, "size_m": 0.1, "R": np.eye(3), "t": [0.0, 0.0, 0.8]}
    rec = run(settings(), frame(cal, [tag]))
    t = rec["tags"][0]
    rx, ry, rz = t["euler_deg"]
    assert abs(rz) < 0.5
    assert abs(rx) < 6 and abs(ry) < 6
    assert t["quat"][0] > 0.99
    assert np.allclose(t["pos_m"], [0, 0, 0.8], atol=0.005)


def test_ambiguity_flags_face_on_tags():
    """Face-on, two tilts explain the corners almost equally well (the
    AprilTag "flip"), so tilt carries degrees of error even on a clean
    synthetic frame. `ambiguity` must say so: high face-on, low oblique."""
    cal = Calibration(K, np.zeros(0), size=(W, H))

    def amb(R):
        rec = run(settings(), frame(cal, [{"id": 0, "size_m": 0.1, "R": R,
                                           "t": [0.0, 0.0, 0.8]}]))
        return rec["tags"][0]["ambiguity"]

    face_on, oblique = amb(np.eye(3)), amb(synth.euler_to_R(0, 25, 0))
    assert face_on > 0.3 and oblique < 0.15 and face_on > 3 * oblique


@pytest.mark.parametrize("euler", [(0, 0, 30), (25, 0, 0), (0, -35, 0), (15, 20, -40)])
def test_rotations_match_ground_truth(euler):
    cal = Calibration(K, np.zeros(0), size=(W, H))
    R = synth.euler_to_R(*euler)
    tag = {"id": 5, "size_m": 0.1, "R": R, "t": [0.1, -0.05, 0.9]}
    rec = run(settings(), frame(cal, [tag]))
    check(rec, [tag])
    assert np.allclose(rec["tags"][0]["euler_deg"], euler, atol=1.5)


def test_detector_pose_agrees_with_ippe():
    """Both pose sources must report the same frame -- the CPU library's
    180-degree flip is undone for either."""
    cal = Calibration(K, np.zeros(0), size=(W, H))
    tag = {"id": 2, "size_m": 0.1, "R": synth.euler_to_R(10, 20, 30),
           "t": [-0.1, 0.05, 1.0]}
    parts = frame(cal, [tag])
    a = run(settings(pose_source="ippe"), parts)["tags"][0]
    b = run(settings(pose_source="detector"), parts)["tags"][0]
    assert rot_err_deg(quat_to_R(a["quat"]), quat_to_R(b["quat"])) < 1.0
    assert np.allclose(a["pos_m"], b["pos_m"], atol=0.005)


# ---- distortion --------------------------------------------------------- #
EDGE_TAG = {"id": 1, "size_m": 0.1, "R": synth.euler_to_R(0, 15, 0),
            "t": [0.45, 0.22, 0.9]}         # well out toward the corner


def barrel_settings(**kw):
    return settings(dist_coeffs=" ".join(map(str, BARREL)), **kw)


@pytest.mark.parametrize("mode", ["image", "points"])
def test_barrel_distortion_is_corrected(mode):
    cal = Calibration(K, BARREL, size=(W, H))
    rec = run(barrel_settings(undistort=mode), frame(cal, [EDGE_TAG]))
    check(rec, [EDGE_TAG])


def test_skipping_correction_is_measurably_wrong():
    """The control for the test above: same frame, correction off. If this
    ever passes the tolerance the synthetic distortion is too weak to prove
    anything."""
    cal = Calibration(K, BARREL, size=(W, H))
    rec = run(barrel_settings(undistort="none"), frame(cal, [EDGE_TAG]))
    if rec["n"]:
        got = rec["tags"][0]["pos_m"]
        assert np.linalg.norm(np.array(got) - EDGE_TAG["t"]) > 0.03


def test_fisheye_model():
    cal = Calibration(K, FISHEYE, model="fisheye", size=(W, H))
    s = settings(dist_coeffs=" ".join(map(str, FISHEYE)), dist_model="equidistant")
    check(run(s, frame(cal, [EDGE_TAG])), [EDGE_TAG])


def test_calibration_at_other_resolution_is_scaled():
    """Calibrated at 1080p, streamed at 720p."""
    cal = Calibration(K, BARREL, size=(W, H))
    s = settings(camera_matrix="1050 1050 959.5 539.5", calib_size="1920x1080",
                 dist_coeffs=" ".join(map(str, BARREL)))
    check(run(s, frame(cal, [EDGE_TAG])), [EDGE_TAG])


def test_reduced_decode_scales_intrinsics():
    cal = Calibration(K, np.zeros(0), size=(W, H))
    tag = {"id": 4, "size_m": 0.15, "R": synth.euler_to_R(0, 20, 0),
           "t": [0.0, 0.0, 0.7]}
    rec = run(settings(decode_scale=2, tag_size_m=0.15), frame(cal, [tag]))
    assert (rec["w"], rec["h"]) == (W // 2, H // 2)
    check(rec, [tag], pos_tol_m=0.015)


# ---- tag selection ------------------------------------------------------ #
def test_per_id_sizes_and_allow_list():
    cal = Calibration(K, np.zeros(0), size=(W, H))
    small = {"id": 3, "size_m": 0.05, "R": np.eye(3), "t": [-0.15, 0.0, 0.6]}
    big = {"id": 7, "size_m": 0.15, "R": np.eye(3), "t": [0.2, 0.0, 1.0]}
    parts = frame(cal, [small, big])
    check(run(settings(tag_sizes="3:0.05,7:0.15"), parts), [small, big])
    rec = run(settings(tag_sizes="3:0.05,7:0.15", tag_ids="7"), parts)
    assert [t["id"] for t in rec["tags"]] == [7]


def test_no_tag_still_produces_a_record():
    cal = Calibration(K, np.zeros(0), size=(W, H))
    rec = run(settings(), frame(cal, []))
    assert rec["n"] == 0 and rec["tags"] == []


def test_corrupt_jpeg_is_counted_not_raised():
    p = Pipeline(settings())
    assert p.process([b"{}", b"\xff\xd8garbage"], 0.0, 1.0) is None
    assert p.stats.n_bad == 1


# ---- record contract ---------------------------------------------------- #
def test_record_shape_and_timing():
    cal = Calibration(K, np.zeros(0), size=(W, H))
    tag = {"id": 0, "size_m": 0.1, "R": np.eye(3), "t": [0.0, 0.0, 1.0]}
    hdr = {"v": 1, "cam": "cam3", "seq": 41, "t_ms": 5000.0, "t_send_ms": 5012.0}
    p = Pipeline(settings())
    rec = p.process(frame(cal, [tag], hdr), 100.0, 1_760_000_000.0, skipped=2)
    assert rec["type"] == "apriltag" and rec["v"] == wire.SCHEMA
    assert (rec["cam"], rec["seq"], rec["tick"], rec["skipped"]) == ("cam3", 41, 1, 2)
    assert rec["clock"] == "est"
    ms, ts = rec["ms"], rec["ts"]
    assert ms["enc"] == pytest.approx(12.0, abs=1e-3)
    assert ms["net"] == pytest.approx(0.0, abs=1e-3)       # first = fastest
    for k in ("decode", "undistort", "detect", "pose", "proc", "total", "e2e"):
        assert ms[k] is not None and ms[k] >= 0, k
    assert ts["cap"] <= ts["send"] <= ts["recv"] <= ts["start"] <= ts["pub"]
    assert rec["t"] == ts["cap"]
    assert ms["e2e"] == pytest.approx((ts["pub"] - ts["cap"]) * 1000, abs=0.01)
    t = rec["tags"][0]
    assert set(t) == {"id", "fam", "size_m", "pos_m", "quat", "euler_deg",
                      "dist_m", "center_px", "corners_px", "hamming", "margin",
                      "err_px", "ambiguity"}
    assert len(t["corners_px"]) == 4 and t["fam"] == "tag36h11"
    # strict JSON: no NaN anywhere
    wire.encode(wire.TOPIC_TAGS, rec)
    p2 = p.process(frame(cal, [tag], dict(hdr, seq=42)), 100.1, 1_760_000_000.1)
    assert p2["tick"] == 2


def test_record_without_sender_timing_falls_back_to_receipt():
    cal = Calibration(K, np.zeros(0), size=(W, H))
    rec = run(settings(), frame(cal, [], header="legacy-name"))
    assert rec["clock"] == "recv" and rec["cam"] == "legacy-name"
    assert rec["t"] == rec["ts"]["recv"]
    assert rec["ms"]["e2e"] is None and rec["ms"]["net"] is None


def test_meta_describes_the_camera_after_first_frame():
    cal = Calibration(K, BARREL, size=(W, H))
    p = Pipeline(barrel_settings(camera_id="bow-cam"))
    assert p.meta()["K_rect"] is None
    p.process(frame(cal, [EDGE_TAG]), 0.0, 1.0)
    m = p.meta()
    assert m["type"] == "apriltag_meta" and m["cam"] == "bow-cam"
    assert m["frame"] == [W, H] and len(m["K_rect"]) == 9
    assert m["calib"]["model"] == "pinhole" and m["calib"]["calibrated"]
    assert "tag_frame" in m["conventions"]
    wire.encode(wire.TOPIC_META, m)


def test_quaternion_and_euler_helpers_round_trip():
    for e in [(0, 0, 0), (10, -20, 30), (-80, 45, 170)]:
        R = synth.euler_to_R(*e)
        assert np.allclose(pose.R_to_euler_deg(R), e, atol=1e-6)
        assert np.allclose(quat_to_R(pose.R_to_quat(R)), R, atol=1e-9)
