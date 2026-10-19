"""tools/viewer.py: pairing, the quaternion helper, and that the overlay lands
on the tag -- through the real Pipeline, so the corner and K_rect conventions
are the detector's own, distorted lens included."""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import viewer  # noqa: E402

from april_detect import synth  # noqa: E402
from april_detect.calib import Calibration, Undistorter  # noqa: E402
from april_detect.pipeline import Pipeline  # noqa: E402

from .test_pipeline import BARREL, H, K, W, frame, quat_to_R, settings  # noqa: E402


# ---- FrameBuffer -------------------------------------------------------- #
def test_frame_buffer_matches_and_evicts_oldest():
    b = viewer.FrameBuffer(3)
    for s in range(5):
        b.put(s, f"f{s}")
    assert len(b) == 3
    assert b.get(0) is None and b.get(1) is None
    assert b.get(4) == "f4"
    assert b.get(None) is None


def test_frame_buffer_across_seq_wrap():
    b = viewer.FrameBuffer(4)
    for s in (2 ** 32 - 2, 2 ** 32 - 1, 0, 1):
        b.put(s, s)
    assert [b.get(s) for s in (2 ** 32 - 2, 2 ** 32 - 1, 0, 1)] == [2 ** 32 - 2, 2 ** 32 - 1, 0, 1]


# ---- helpers ------------------------------------------------------------ #
def test_identity_quat_is_zero_rvec():
    assert np.allclose(viewer.quat_to_rvec([1, 0, 0, 0]), 0)


def test_quat_to_R_agrees_with_the_test_suite():
    q = np.array([0.9, 0.1, -0.3, 0.2])
    q /= np.linalg.norm(q)
    assert np.allclose(viewer.quat_to_R(q), quat_to_R(q))
    R, _ = cv2.Rodrigues(viewer.quat_to_rvec(q))
    assert np.allclose(R, quat_to_R(q))


def test_scale_points_uses_pixel_centres():
    p = viewer.scale_points([[0, 0], [639.5, 359.5]], (640, 360), (1280, 720))
    assert np.allclose(p, [[0.5, 0.5], [1279.5, 719.5]])


# ---- overlay lands on the tag ------------------------------------------- #
def _scene():
    return [{"id": 3, "size_m": 0.10, "R": synth.euler_to_R(10, 25, 5),
             "t": [0.05, -0.03, 0.9]}]


def _detect(cal: Calibration, **kw):
    pipe = Pipeline(settings(**kw))
    rec = pipe.process(frame(cal, _scene()), 0.0, 1_760_000_000.0)
    assert rec is not None and rec["n"] == 1
    return rec, pipe.meta()


# Corner order in the *reported* tag frame: pose.object_points() is in the
# library's native frame, which is the reported one turned 180 deg about z.
CORNERS = np.float64([[1, -1, 0], [-1, -1, 0], [-1, 1, 0], [1, 1, 0]])


def _true_corners(cal: Calibration, K_rect: np.ndarray) -> np.ndarray:
    """Where the tag's corners are in the corrected image, from ground truth."""
    tg = _scene()[0]
    h = tg["size_m"] / 2
    obj = CORNERS * h
    rvec, _ = cv2.Rodrigues(tg["R"])
    p, _ = cv2.projectPoints(obj, rvec, np.float64(tg["t"]), K_rect, None)
    return p.reshape(-1, 2)


@pytest.mark.parametrize("decode_scale", [1, 2])
def test_overlay_on_corrected_frame(decode_scale):
    """Barrel lens, possibly a reduced decode on the detector: the drawn
    outline sits on the true corners of the corrected full-size frame."""
    cal = Calibration(K, BARREL, size=(W, H))
    rec, meta = _detect(cal, dist_coeffs=" ".join(map(str, BARREL)),
                        decode_scale=decode_scale)
    proj = viewer.Projector(meta, (W, H))
    assert proj.meta_ok and proj.maps is not None

    shown = proj.corners(rec["tags"][0]["corners_px"], (rec["w"], rec["h"]))
    truth = _true_corners(cal, Undistorter(cal, "image").K_rect)
    assert np.abs(shown - truth).max() < 1.0 * decode_scale

    raw = cv2.cvtColor(synth.render(cal, _scene()), cv2.COLOR_GRAY2BGR)
    img = proj.image(raw)
    before = img.copy()
    viewer.annotate(img, rec, proj)
    changed = np.any(img != before, axis=2)
    for x, y in np.round(truth).astype(int):
        assert changed[y - 2:y + 3, x - 2:x + 3].any(), (x, y)


def test_overlay_raw_view_distorts_corners_back():
    cal = Calibration(K, BARREL, size=(W, H))
    rec, meta = _detect(cal, dist_coeffs=" ".join(map(str, BARREL)))
    proj = viewer.Projector(meta, (W, H), raw=True)
    assert proj.raw and proj.maps is None

    shown = proj.corners(rec["tags"][0]["corners_px"], (W, H))
    tg = _scene()[0]
    h = tg["size_m"] / 2
    obj = CORNERS * h
    rvec, _ = cv2.Rodrigues(tg["R"])
    truth, _ = cv2.projectPoints(obj, rvec, np.float64(tg["t"]), K, BARREL)
    assert np.abs(shown - truth.reshape(-1, 2)).max() < 1.0


def test_no_meta_still_draws_corners():
    cal = Calibration(K, np.zeros(0), size=(W, H))
    rec, _ = _detect(cal)
    proj = viewer.Projector(None, (W, H))
    assert not proj.meta_ok
    img = np.zeros((H, W, 3), np.uint8)
    viewer.annotate(img, rec, proj)
    assert img.any()
