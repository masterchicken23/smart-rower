#!/usr/bin/env python3
"""
Camera model and distortion correction.

Calibration comes from configuration, either inline (APRIL_CAMERA_MATRIX and
friends, convenient in a compose file) or from a file (APRIL_CALIB_PATH):

    .yaml / .yml / .json   ROS camera_info layout, which is what
                           `camera_calibration` and most calibration tools write:
                             image_width, image_height,
                             camera_matrix: {data: [9 floats, row-major]},
                             distortion_model: plumb_bob | rational_polynomial
                                               | equidistant,
                             distortion_coefficients: {data: [...]}
    .npz                   the layout experiments/april-tag used: K (3x3),
                           optional dist / D, optional image_size = (w, h)

Two distortion models, because the cameras on the boat are not settled yet:
`pinhole` (OpenCV's standard Brown-Conrady, 4/5/8/12/14 coefficients; ROS calls
it plumb_bob or rational_polynomial) and `fisheye` (Kannala-Brandt, 4
coefficients; ROS `equidistant`). A wide lens on the Pi camera needs the second.

Calibrated at one resolution and streamed at another is handled by scaling K;
distortion coefficients live in normalised coordinates and carry over, provided
the aspect ratio -- and so the sensor crop -- is the same. A changed aspect
ratio means a different crop of the sensor, which no rescale can fix, so it is
refused with a warning and the frame is treated as uncalibrated.

Undistort modes (APRIL_UNDISTORT):

    image    remap every frame to an ideal pinhole image, then detect. The
             pipeline as specified, and the right choice for a wide lens:
             AprilTag finds quads by fitting straight edges, and barrel
             distortion bends them, so detection itself degrades near the
             frame edge if the image is not corrected first.
    points   detect on the raw frame and undistort only the four corners of
             each detection before solving pose. Same pose accuracy for mild
             distortion at none of the per-pixel cost; loses detections near
             the edge of a strongly distorted frame.
    none     treat the camera as an ideal pinhole (D ignored).

The remap uses precomputed fixed-point maps (CV_16SC2). It is memory-bound --
nearest-neighbour is barely cheaper than bilinear -- and costs about as much as
the JPEG decode: ~3.4 ms for 720p grey on one laptop core, so expect ~3-4x
that on an Orin Nano core. `points` mode is the lever if that matters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from . import log

MODELS = ("pinhole", "fisheye")
_ROS_MODELS = {
    "plumb_bob": "pinhole",
    "rational_polynomial": "pinhole",
    "pinhole": "pinhole",
    "radtan": "pinhole",
    "equidistant": "fisheye",
    "fisheye": "fisheye",
    "kannala_brandt": "fisheye",
}


@dataclass
class Calibration:
    """K, D and the resolution they are valid for. `size` is (w, h); None
    means "whatever arrives", used only for the uncalibrated fallback."""

    K: np.ndarray
    D: np.ndarray
    model: str = "pinhole"
    size: Optional[tuple[int, int]] = None
    source: str = "config"
    calibrated: bool = True

    @property
    def has_distortion(self) -> bool:
        return bool(self.D.size) and bool(np.any(self.D != 0))

    def scaled_to(self, w: int, h: int) -> Calibration:
        """This calibration at another resolution of the same sensor crop."""
        if self.size is None or self.size == (w, h):
            return Calibration(self.K.copy(), self.D.copy(), self.model, (w, h),
                               self.source, self.calibrated)
        cw, ch = self.size
        if abs(w / h - cw / ch) > 0.01:
            raise ValueError(
                f"frame is {w}x{h} but calibration is {cw}x{ch}: different "
                f"aspect ratio, so a different sensor crop; recalibrate")
        sx, sy = w / cw, h / ch
        K = self.K.copy()
        K[0, 0] *= sx
        K[0, 2] = (K[0, 2] + 0.5) * sx - 0.5        # pixel-centre convention
        K[1, 1] *= sy
        K[1, 2] = (K[1, 2] + 0.5) * sy - 0.5
        return Calibration(K, self.D.copy(), self.model, (w, h), self.source,
                           self.calibrated)

    def describe(self) -> dict[str, Any]:
        return {
            "K": [round(float(v), 4) for v in self.K.ravel()],
            "D": [float(v) for v in self.D.ravel()],
            "model": self.model,
            "size": list(self.size) if self.size else None,
            "source": self.source,
            "calibrated": self.calibrated,
        }


def K_from(fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    return np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]])


def parse_floats(text: str) -> list[float]:
    """'1, 2 3' -> [1.0, 2.0, 3.0]. Commas or whitespace, so a compose entry
    needs no JSON quoting."""
    return [float(t) for t in text.replace(",", " ").split()]


def parse_size(text: str) -> Optional[tuple[int, int]]:
    """'1280x720' -> (1280, 720); '' -> None."""
    text = text.strip().lower()
    if not text:
        return None
    w, h = text.replace(",", "x").split("x")
    return int(w), int(h)


def normalise_model(name: str) -> str:
    m = _ROS_MODELS.get(name.strip().lower())
    if m is None:
        raise ValueError(f"unknown distortion model {name!r}; expected one of "
                         f"{sorted(_ROS_MODELS)}")
    return m


def from_inline(camera_matrix: str, dist_coeffs: str, model: str,
                size: str) -> Calibration:
    """APRIL_CAMERA_MATRIX accepts 4 values (fx fy cx cy) or 9 (row-major K)."""
    v = parse_floats(camera_matrix)
    if len(v) == 4:
        K = K_from(*v)
    elif len(v) == 9:
        K = np.array(v, float).reshape(3, 3)
    else:
        raise ValueError("APRIL_CAMERA_MATRIX needs 4 values (fx fy cx cy) "
                         f"or 9 (row-major K), got {len(v)}")
    D = np.array(parse_floats(dist_coeffs), float)
    return Calibration(K, D, normalise_model(model), parse_size(size), "env")


def from_file(path: str) -> Calibration:
    p = Path(path)
    if p.suffix == ".npz":
        d = np.load(p)
        K = next((np.asarray(d[k], float) for k in ("K", "camera_matrix", "mtx")
                  if k in d), None)
        if K is None:
            raise ValueError(f"{p}: needs a 3x3 'K' / 'camera_matrix' / 'mtx'")
        D = next((np.asarray(d[k], float).ravel() for k in ("D", "dist", "dist_coeffs")
                  if k in d), np.zeros(0))
        size = tuple(int(v) for v in d["image_size"]) if "image_size" in d else None
        model = normalise_model(str(d["model"])) if "model" in d else "pinhole"
        return Calibration(K.reshape(3, 3), D, model, size, str(p))  # type: ignore[arg-type]

    import yaml  # parses JSON too

    with open(p, encoding="utf-8") as f:
        y = yaml.safe_load(f)
    cm = y["camera_matrix"]
    K = np.array(cm["data"] if isinstance(cm, dict) else cm, float).reshape(3, 3)
    dc = y.get("distortion_coefficients", [])
    D = np.array(dc["data"] if isinstance(dc, dict) else dc, float).ravel()
    model = normalise_model(y.get("distortion_model", "plumb_bob"))
    size = None
    if "image_width" in y and "image_height" in y:
        size = (int(y["image_width"]), int(y["image_height"]))
    return Calibration(K, D, model, size, str(p))


def fallback(w: int, h: int, hfov_deg: float) -> Calibration:
    """No calibration configured: an ideal pinhole with the given horizontal
    field of view. Positions will be off by however wrong the guess is, so the
    record says calibrated=false (in meta) and a warning is logged."""
    f = (w / 2) / math.tan(math.radians(hfov_deg) / 2)
    return Calibration(K_from(f, f, (w - 1) / 2, (h - 1) / 2), np.zeros(0),
                       "pinhole", (w, h), f"fallback hfov={hfov_deg:g}",
                       calibrated=False)


# --------------------------------------------------------------------------- #
class Undistorter:
    """Corrects one resolution's frames, or its detections' corners.

    `K_rect` is the camera matrix of the corrected image: the intrinsics that
    detected corners are expressed in, and the ones pose is solved with.
    """

    def __init__(self, calib: Calibration, mode: str, alpha: float = -1.0) -> None:
        import cv2

        assert calib.size is not None
        self.calib = calib
        self.mode = mode if calib.has_distortion else "none"
        w, h = calib.size
        K, D = calib.K, calib.D
        self.K_rect = K.copy()
        self.maps: Optional[tuple[np.ndarray, np.ndarray]] = None

        if self.mode == "image":
            fisheye = calib.model == "fisheye"
            if alpha >= 0:
                # Choose how much of the source survives: 0 crops to valid
                # pixels only, 1 keeps every source pixel (black corners).
                if fisheye:
                    self.K_rect = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
                        K, D, (w, h), np.eye(3), balance=alpha)
                else:
                    self.K_rect, _ = cv2.getOptimalNewCameraMatrix(
                        K, D, (w, h), alpha, (w, h))
            init = (cv2.fisheye.initUndistortRectifyMap if fisheye
                    else cv2.initUndistortRectifyMap)
            self.maps = init(K, D, np.eye(3), self.K_rect, (w, h), cv2.CV_16SC2)

    def image(self, gray: np.ndarray) -> np.ndarray:
        """The corrected frame (or the same frame, when nothing to do)."""
        if self.maps is None:
            return gray
        import cv2

        # Border handling left at the default (constant 0): passing the same
        # thing explicitly measured ~1.2 ms slower per 720p frame.
        return cv2.remap(gray, self.maps[0], self.maps[1], cv2.INTER_LINEAR)

    def points(self, px: np.ndarray) -> np.ndarray:
        """Raw-frame pixel coordinates (N, 2) -> corrected-frame coordinates.
        Identity unless mode is `points`, where it does the actual work."""
        if self.mode != "points":
            return px
        import cv2

        pts = np.asarray(px, np.float64).reshape(-1, 1, 2)
        if self.calib.model == "fisheye":
            out = cv2.fisheye.undistortPoints(pts, self.calib.K, self.calib.D,
                                              P=self.K_rect)
        else:
            out = cv2.undistortPoints(pts, self.calib.K, self.calib.D,
                                      P=self.K_rect)
        return out.reshape(-1, 2)


class CameraModel:
    """Resolves the configured calibration against whatever resolution actually
    arrives, and keeps one Undistorter per resolution in use. The maps are
    built once -- the first frame at a new size pays tens of milliseconds,
    every later frame pays only the remap."""

    def __init__(self, base: Optional[Calibration], mode: str, alpha: float,
                 hfov_deg: float) -> None:
        self.base = base
        self.mode = mode
        self.alpha = alpha
        self.hfov_deg = hfov_deg
        self._cache: dict[tuple[int, int], Undistorter] = {}

    def for_size(self, w: int, h: int) -> Undistorter:
        u = self._cache.get((w, h))
        if u is not None:
            return u
        calib: Optional[Calibration] = None
        if self.base is not None:
            try:
                calib = self.base.scaled_to(w, h)
            except ValueError as e:
                log.warn(f"[calib] {e}; falling back to an uncalibrated pinhole")
        if calib is None:
            calib = fallback(w, h, self.hfov_deg)
            log.warn(f"[calib] no usable calibration for {w}x{h}; assuming "
                     f"{self.hfov_deg:g} deg HFOV. Positions are approximate.")
        u = Undistorter(calib, self.mode, self.alpha)
        Kr = u.K_rect
        log.info(f"[calib] {w}x{h} model={calib.model} undistort={u.mode} "
                 f"fx={Kr[0, 0]:.1f} fy={Kr[1, 1]:.1f} cx={Kr[0, 2]:.1f} "
                 f"cy={Kr[1, 2]:.1f} ({calib.source})")
        self._cache[(w, h)] = u
        return u
