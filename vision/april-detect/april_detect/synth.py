#!/usr/bin/env python3
"""
Synthetic camera frames: a tag at a known pose, seen through a known (and
optionally distorted) camera.

Used by the tests to check the pose convention and the undistortion end to end
against ground truth, and by tools/fake_camera.py to drive a real container
with no camera attached. Rendering is plain projective geometry -- the tag
texture is warped by K [r1 r2 t] -- and distortion is applied afterwards with a
remap, which is the exact inverse of what the pipeline undoes.

Tags are rendered with OpenCV's aruco module, which carries the AprilTag 36h11,
25h9, 16h5 and 36h10 dictionaries bit-for-bit.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .calib import Calibration

ARUCO_DICTS = {
    "tag36h11": "DICT_APRILTAG_36h11",
    "tag36h10": "DICT_APRILTAG_36h10",
    "tag25h9": "DICT_APRILTAG_25h9",
    "tag16h5": "DICT_APRILTAG_16h5",
}


def tag_texture(tag_id: int, family: str = "tag36h11", px_per_bit: int = 20,
                margin_bits: int = 2) -> tuple[np.ndarray, int]:
    """Grayscale tag on a white margin. Returns (image, black-square edge px).

    The black square -- the region whose edge length is the AprilTag "tag
    size" -- is centred in the image.
    """
    import cv2

    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICTS[family]))
    bits = d.markerSize + 2                         # data plus 1-bit black border
    edge = bits * px_per_bit
    marker = cv2.aruco.generateImageMarker(d, tag_id, edge, borderBits=1)
    m = margin_bits * px_per_bit
    return cv2.copyMakeBorder(marker, m, m, m, m, cv2.BORDER_CONSTANT,
                              value=255), edge


def euler_to_R(rx_deg: float, ry_deg: float, rz_deg: float) -> np.ndarray:
    """R = Rz @ Ry @ Rx -- the inverse of pose.R_to_euler_deg."""
    rx, ry, rz = (math.radians(a) for a in (rx_deg, ry_deg, rz_deg))
    cx, sx, cy, sy, cz, sz = (math.cos(rx), math.sin(rx), math.cos(ry),
                              math.sin(ry), math.cos(rz), math.sin(rz))
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def render(calib: Calibration, tags: list[dict], background: int = 160,
           noise: float = 0.0, seed: Optional[int] = None) -> np.ndarray:
    """A grayscale frame of `calib.size` showing each tag at its pose.

    Each entry of `tags` is {"id", "size_m", "R", "t"} with R, t mapping the tag
    frame into the camera frame, in the convention this service reports: tag
    origin at its centre, +x right, +y down, +z into the tag (away from a camera
    facing it). The image is distorted by calib.D under calib.model.
    """
    import cv2

    w, h = calib.size
    K = calib.K
    img = np.full((h, w), background, np.uint8)
    for tg in tags:
        tex, edge = tag_texture(tg["id"], tg.get("family", "tag36h11"))
        c = tex.shape[0] / 2.0
        s = tg["size_m"] / edge                      # metres per texture px
        # texture px -> tag-frame metres (z = 0 plane)
        A = np.array([[s, 0, -c * s], [0, s, -c * s], [0, 0, 1]])
        R, t = np.asarray(tg["R"], float), np.asarray(tg["t"], float).ravel()
        H = K @ np.column_stack([R[:, 0], R[:, 1], t]) @ A
        mask = cv2.warpPerspective(np.full_like(tex, 255), H, (w, h),
                                   flags=cv2.INTER_NEAREST)
        warped = cv2.warpPerspective(tex, H, (w, h), flags=cv2.INTER_AREA)
        img[mask > 0] = warped[mask > 0]

    if calib.has_distortion:
        mx, my = _distort_maps(calib)
        img = cv2.remap(img, mx, my, cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=background)

    if noise > 0:
        rng = np.random.default_rng(seed)
        img = np.clip(img + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)
    return img


_MAPS: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}


def _distort_maps(calib: Calibration) -> tuple[np.ndarray, np.ndarray]:
    """For each pixel of the distorted output, where it lands in the ideal
    image. Cached: building it undistorts every pixel, ~100 ms at 720p."""
    import cv2

    key = (calib.K.tobytes(), calib.D.tobytes(), calib.model, calib.size)
    if key in _MAPS:
        return _MAPS[key]
    w, h = calib.size  # type: ignore[misc]
    u, v = np.meshgrid(np.arange(w, dtype=np.float32),
                       np.arange(h, dtype=np.float32))
    pts = np.stack([u.ravel(), v.ravel()], axis=1).reshape(-1, 1, 2)
    if calib.model == "fisheye":
        und = cv2.fisheye.undistortPoints(pts, calib.K, calib.D, P=calib.K)
    else:
        und = cv2.undistortPoints(pts, calib.K, calib.D, P=calib.K)
    maps = (und[:, 0, 0].reshape(h, w).astype(np.float32),
            und[:, 0, 1].reshape(h, w).astype(np.float32))
    _MAPS[key] = maps
    return maps


def scene_at(seq: int, size_m: float = 0.10, tag_id: int = 0,
             family: str = "tag36h11") -> list[dict]:
    """A deterministic, slowly moving pose for frame `seq`, so a consumer that
    knows only `seq` can recompute ground truth. Roughly a seat travelling on a
    slide at ~0.5 Hz, seen side-on from a metre away, with a little wobble."""
    ph = 2 * math.pi * 0.5 * (seq / 15.0)
    t = [0.15 * math.sin(ph), 0.02 * math.sin(2 * ph), 1.0 + 0.1 * math.cos(ph)]
    R = euler_to_R(15 * math.sin(ph), 20 * math.cos(ph), 10 * math.sin(0.5 * ph))
    return [{"id": tag_id, "size_m": size_m, "family": family, "R": R, "t": t}]
