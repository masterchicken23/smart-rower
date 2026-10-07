#!/usr/bin/env python3
"""
Tag pose: from four corners to a position and rotation in the camera frame.

Conventions (reported in every meta record, and pinned by tests/test_pose.py):

    camera frame   OpenCV: +x right, +y down, +z out of the lens.
    tag frame      origin at the tag's centre; +x toward the tag's right edge,
                   +y toward its bottom edge, +z into the tag, as the tag is
                   read upright. An upright tag squarely facing the camera has
                   R = identity, and so quat = [1, 0, 0, 0] and euler = 0/0/0.
    pos_m          the tag origin in the camera frame, metres.
    R / quat       rotate tag-frame vectors into the camera frame:
                   p_cam = R @ p_tag + pos.

The AprilTag library's own frame is rotated 180 degrees about z from this one
for an upright tag (tag +x points left). That was found by hand in
experiments/april-tag and is checked here against synthetic ground truth;
FLIP_Z180 undoes it, so every backend reports the same frame.

Pose is solved with OpenCV's IPPE_SQUARE by default rather than taken from the
detector, for three reasons: it is the same solver whichever detector found the
corners (one convention to verify, not two); it takes the tag size per id, so
mixed tag sizes work; and it returns both of the planar-pose candidates with
their reprojection errors. A small, distant or face-on tag has two poses that
explain its corners almost equally well -- the well-known AprilTag "flip" --
and `ambiguity` (best error / second-best error) says when that is happening:
near 1.0, the reported rotation should not be trusted. It costs ~30 us a tag.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np

FLIP_Z180 = np.diag([-1.0, -1.0, 1.0])


def object_points(size_m: float) -> np.ndarray:
    """Tag corners in the AprilTag library's corner order, which is also the
    order SOLVEPNP_IPPE_SQUARE requires."""
    s = size_m / 2.0
    return np.array([[-s, s, 0.0], [s, s, 0.0], [s, -s, 0.0], [-s, -s, 0.0]])


def solve(corners_px: np.ndarray, size_m: float,
          K: np.ndarray) -> Optional[dict[str, Any]]:
    """Corners (4, 2) in an undistorted image with intrinsics K -> pose.

    Returns R (in this module's convention), t, the RMS reprojection error of
    the chosen solution in pixels, and the ambiguity ratio. None if OpenCV
    finds no solution (degenerate corners)."""
    import cv2

    img = np.ascontiguousarray(corners_px, np.float64).reshape(4, 1, 2)
    try:
        n, rvecs, tvecs, errs = cv2.solvePnPGeneric(
            object_points(size_m), img, K, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
    except cv2.error:
        return None
    if not n:
        return None
    e = [float(v) for v in np.asarray(errs).ravel()]
    best = int(np.argmin(e))
    R, _ = cv2.Rodrigues(rvecs[best])
    amb = None
    if len(e) > 1:
        other = max(e[i] for i in range(len(e)) if i != best)
        amb = e[best] / other if other > 0 else 1.0
    return {"R": R @ FLIP_Z180, "t": np.asarray(tvecs[best], float).ravel(),
            "err_px": e[best], "ambiguity": amb}


def R_to_quat(R: np.ndarray) -> list[float]:
    """Rotation matrix -> unit quaternion [w, x, y, z], w >= 0."""
    m = R
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        w, x = 0.25 * s, (m[2, 1] - m[1, 2]) / s
        y, z = (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w, x = (m[2, 1] - m[1, 2]) / s, 0.25 * s
        y, z = (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w, x = (m[0, 2] - m[2, 0]) / s, (m[0, 1] + m[1, 0]) / s
        y, z = 0.25 * s, (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w, x = (m[1, 0] - m[0, 1]) / s, (m[0, 2] + m[2, 0]) / s
        y, z = (m[1, 2] + m[2, 1]) / s, 0.25 * s
    q = np.array([w, x, y, z])
    q /= np.linalg.norm(q)
    if q[0] < 0:
        q = -q
    return [float(v) for v in q]


def R_to_euler_deg(R: np.ndarray) -> list[float]:
    """[rx, ry, rz] such that R = Rz(rz) @ Ry(ry) @ Rx(rx), in degrees.

    Same decomposition experiments/april-tag printed: rx tilts the tag
    toward/away about camera x, ry turns it left/right about camera y, rz spins
    it in the image plane. Convenience for humans only -- it has a singularity
    at ry = +-90 deg, so anything computational should use `quat`.
    """
    sy = math.hypot(R[0, 0], R[1, 0])
    if sy > 1e-6:
        rx = math.atan2(R[2, 1], R[2, 2])
        ry = math.atan2(-R[2, 0], sy)
        rz = math.atan2(R[1, 0], R[0, 0])
    else:                                           # gimbal lock
        rx = math.atan2(-R[1, 2], R[1, 1])
        ry = math.atan2(-R[2, 0], sy)
        rz = 0.0
    return [math.degrees(rx), math.degrees(ry), math.degrees(rz)]
