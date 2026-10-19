#!/usr/bin/env python3
"""
A rower's tag measured against the boat's reference tag, in metres.

Pixels are not a length: the same 10 px is a centimetre near the lens and a
decimetre across the boat. What turns them into metres is the known tag size.
pose.solve() fits each tag's four corners to a square of that size through the
camera intrinsics, which gives the tag's centre in the camera frame in metres,
depth included. Everything here is arithmetic on those two positions, so it is
only as good as the calibration and undistortion upstream of it (calib.py) and
needs nothing from them directly.

Given the reference tag's pose (R_ref, p_ref) and the rower tag's p_oar, both
in the camera frame:

    dist_m     |p_oar - p_ref|. Needs no rotation, so it holds even when the
               reference tag's rotation is unreliable (the AprilTag "flip" on a
               small or face-on tag spoils rotation, not position).
    rel_m      R_ref^T (p_oar - p_ref): the rower tag in the reference tag's own
               frame, so it stays put if the camera is knocked, as long as the
               reference tag is fixed to the boat.
    height_m   rel_m along the reference tag's up axis (APRIL_REF_UP_AXIS). Tag
               frame is +x right, +y down, +z into the tag (pose.py), so "-y"
               for a reference mounted upright on a vertical face and "-z" for
               one lying face-up on the gunwale. Unlike dist_m this depends on
               the reference rotation; `ref_ambiguity` near 1 says not to
               trust it.
    err_m      a lower bound on dist_m's uncertainty from the pose fits alone.
               Depth is the weak axis of a single-tag pose, and it is fixed by
               apparent size: z * err_px / edge_px per tag, the two tags in
               quadrature. It does not see calibration error, so treat it as a
               floor, not a confidence interval. None without a fit residual
               (APRIL_POSE_SOURCE=detector).

The output is the `oar` object funnel/compute/form.py specifies, plus the
fields above. Either tag out of view gives None, never an extrapolation.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np

UP_AXES: dict[str, tuple[float, float, float]] = {
    "+x": (1.0, 0.0, 0.0), "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0), "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0), "-z": (0.0, 0.0, -1.0),
}


def quat_to_R(q: list[float]) -> np.ndarray:
    """[w, x, y, z] -> 3x3 rotation; the inverse of pose.R_to_quat."""
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _find(tags: list[dict[str, Any]], tag_id: int) -> Optional[dict]:
    return next((t for t in tags if t.get("id") == tag_id), None)


def _edge_px(corners: list[list[float]]) -> float:
    c = np.asarray(corners, float)
    return float(np.mean(np.linalg.norm(c - np.roll(c, 1, axis=0), axis=1)))


def _depth_err_m(t: dict[str, Any]) -> Optional[float]:
    err, dist = t.get("err_px"), t.get("dist_m")
    if err is None or dist is None:
        return None
    edge = _edge_px(t["corners_px"])
    return dist * err / edge if edge > 0 else None


def _r(v: Optional[float], n: int = 4) -> Optional[float]:
    return None if v is None else round(float(v), n)


def oar(tags: list[dict[str, Any]], oar_id: int, ref_id: int,
        up_axis: str) -> Optional[dict[str, Any]]:
    """The rower tag against the reference tag, from one frame's tag records
    (pipeline._tag's shape). None unless both are in view."""
    ref, mov = _find(tags, ref_id), _find(tags, oar_id)
    if ref is None or mov is None:
        return None
    d_cam = np.asarray(mov["pos_m"], float) - np.asarray(ref["pos_m"], float)
    rel = quat_to_R(ref["quat"]).T @ d_cam
    errs = (_depth_err_m(ref), _depth_err_m(mov))
    err = None if None in errs else math.hypot(*errs)
    return {
        "oar_id": oar_id,
        "ref_id": ref_id,
        "dist_m": _r(np.linalg.norm(d_cam)),
        "rel_m": [_r(v) for v in rel],
        "height_m": _r(rel @ np.asarray(UP_AXES[up_axis])),
        "up_axis": up_axis,
        "err_m": _r(err),
        "ref_ambiguity": ref.get("ambiguity"),
    }
