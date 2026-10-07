#!/usr/bin/env python3
"""
Detector backends. Each takes a grayscale frame and returns Detections.

    cpu    pupil_apriltags -- the AprilTag 3 C library, multithreaded. Any
           family. The default, and the one the throughput figures in the
           README are built on: it leaves the GPU to the pose model.
    cuda   NVIDIA cuAprilTags (the detector inside Isaac ROS), loaded with
           ctypes from a libcuapriltags.so supplied at runtime. tag36h11 only.
           Ported from experiments/april-tag/apriltag_zmq_tracker.py.

`auto` tries cuda and falls back to cpu, logging why.

Both get the same grayscale, already-undistorted frame. cuAprilTags wants
packed RGB in device memory, so the grey plane is expanded to three channels
just before upload -- it only ever looks at luminance, so this loses nothing,
and it lets undistortion run on one plane instead of three.

On the CUDA backend the pose is cuAprilTags' own: its corner order has not been
checked against the synthetic ground truth in tests/ (that needs the library
and a GPU), whereas its pose output was checked by hand in experiments/april-
tag. tools/fake_camera.py + tools/tap.py --truth verify it on the Jetson.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from . import log
from .config import CUDA_FAMILIES, Settings
from .pose import FLIP_Z180


@dataclass(slots=True)
class Detection:
    id: int
    family: str
    corners: np.ndarray
    """(4, 2) pixels in the frame given to detect(), AprilTag corner order."""
    hamming: int
    margin: Optional[float] = None
    """Decision margin -- how far the decode was from ambiguous. CPU only."""
    R: Optional[np.ndarray] = None
    """Detector's own pose, already in pose.py's convention, for `size_m`."""
    t: Optional[np.ndarray] = None
    size_m: Optional[float] = None


# --------------------------------------------------------------------------- #
class PupilDetector:
    name = "cpu"
    corner_order_verified = True

    def __init__(self, s: Settings) -> None:
        from pupil_apriltags import Detector

        self.det = Detector(families=" ".join(s.family_list),
                            nthreads=max(1, s.threads),
                            quad_decimate=s.decimate, quad_sigma=s.sigma,
                            refine_edges=int(s.refine_edges),
                            decode_sharpening=s.decode_sharpening)
        self.native_pose = s.pose_source == "detector"
        self.tag_size = s.tag_size_m

    def describe(self) -> str:
        return "cpu (pupil_apriltags)"

    def detect(self, gray: np.ndarray, K: np.ndarray) -> list[Detection]:
        kw: dict[str, Any] = {}
        if self.native_pose:
            kw = {"estimate_tag_pose": True,
                  "camera_params": (K[0, 0], K[1, 1], K[0, 2], K[1, 2]),
                  "tag_size": self.tag_size}
        out = []
        for d in self.det.detect(gray, **kw):
            fam = d.tag_family.decode() if isinstance(d.tag_family, bytes) else str(d.tag_family)
            det = Detection(int(d.tag_id), fam, np.asarray(d.corners, np.float64),
                            int(d.hamming), float(d.decision_margin))
            if self.native_pose and d.pose_R is not None:
                det.R = np.asarray(d.pose_R) @ FLIP_Z180
                det.t = np.asarray(d.pose_t, float).ravel()
                det.size_m = self.tag_size
            out.append(det)
        return out

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
class CudaRT:
    """Just enough of libcudart via ctypes to upload a frame."""
    H2D = 1

    def __init__(self) -> None:
        self.lib = None
        for name in ("libcudart.so", "libcudart.so.12", "libcudart.so.11.0",
                     "/usr/local/cuda/lib64/libcudart.so"):
            try:
                # RTLD_GLOBAL so libcuapriltags.so can resolve CUDA symbols.
                self.lib = ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL)
                break
            except OSError:
                continue
        if self.lib is None:
            raise OSError("libcudart not found (container needs runtime: nvidia "
                          "and the CUDA image; see Dockerfile.cuda)")
        L = self.lib
        L.cudaMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
        L.cudaFree.argtypes = [ctypes.c_void_p]
        L.cudaMemcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.c_int]
        L.cudaGetErrorString.restype = ctypes.c_char_p
        self._buf: Optional[ctypes.c_void_p] = None
        self._cap = 0

    def _check(self, rc: int, what: str) -> None:
        if rc != 0:
            raise RuntimeError(f"{what} failed: "
                               f"{self.lib.cudaGetErrorString(rc).decode()}")  # type: ignore[union-attr]

    def upload(self, host: np.ndarray) -> int:
        """Copy a contiguous array into a reusable device buffer."""
        L = self.lib
        n = host.nbytes
        if n > self._cap:
            if self._buf:
                L.cudaFree(self._buf)  # type: ignore[union-attr]
            p = ctypes.c_void_p()
            self._check(L.cudaMalloc(ctypes.byref(p), n), "cudaMalloc")  # type: ignore[union-attr]
            self._buf, self._cap = p, n
        self._check(L.cudaMemcpy(self._buf, host.ctypes.data, n, self.H2D),  # type: ignore[union-attr]
                    "cudaMemcpy")
        return self._buf.value  # type: ignore[union-attr,return-value]


class _Float2(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]


class _CuTag(ctypes.Structure):
    # Mirrors cuAprilTagsID_t. CUDA's float2 is 8-byte aligned, so the C struct
    # is padded to 88 bytes; ctypes aligns float2 to 4, hence the explicit pad.
    _fields_ = [("corners", _Float2 * 4),
                ("id", ctypes.c_uint16),
                ("hamming_error", ctypes.c_uint8),
                ("orientation", ctypes.c_float * 9),    # 3x3, column-major
                ("translation", ctypes.c_float * 3),    # units of tag_size
                ("_pad", ctypes.c_uint32)]


class _CuImage(ctypes.Structure):
    _fields_ = [("dev_ptr", ctypes.c_void_p),            # uchar3*, packed RGB
                ("pitch", ctypes.c_size_t),
                ("width", ctypes.c_uint16),
                ("height", ctypes.c_uint16)]


class _CuIntrinsics(ctypes.Structure):
    _fields_ = [("fx", ctypes.c_float), ("fy", ctypes.c_float),
                ("cx", ctypes.c_float), ("cy", ctypes.c_float)]


assert ctypes.sizeof(_CuTag) == 88 and ctypes.sizeof(_CuImage) == 24


class CuAprilTagsDetector:
    name = "cuda"
    corner_order_verified = False
    native_pose = True
    FAMILIES = {"tag36h11": 0}

    def __init__(self, s: Settings, max_tags: int = 32, tile_size: int = 4) -> None:
        bad = [f for f in s.family_list if f not in CUDA_FAMILIES]
        if bad:
            raise ValueError(f"cuAprilTags supports {list(CUDA_FAMILIES)} only, "
                             f"not {bad}")
        self.cuda = CudaRT()                        # must load first
        self.lib = ctypes.CDLL(s.cuapriltags_lib)
        L = self.lib
        L.nvCreateAprilTagsDetector.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_int32, ctypes.POINTER(_CuIntrinsics),
            ctypes.c_float]
        L.cuAprilTagsDetect.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_CuImage), ctypes.POINTER(_CuTag),
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32, ctypes.c_void_p]
        L.cuAprilTagsDestroy.argtypes = [ctypes.c_void_p]
        self.tag_size = float(s.tag_size_m)
        self.max_tags, self.tile = max_tags, tile_size
        self.out = (_CuTag * max_tags)()
        self.handle: Optional[ctypes.c_void_p] = None
        self.cfg: Optional[tuple] = None

    def describe(self) -> str:
        return "cuda (cuAprilTags)"

    def _ensure(self, w: int, h: int, K: np.ndarray) -> None:
        cfg = (w, h, float(K[0, 0]), float(K[1, 1]), float(K[0, 2]), float(K[1, 2]))
        if cfg == self.cfg:
            return
        self.close()
        cam = _CuIntrinsics(*cfg[2:])
        hnd = ctypes.c_void_p()
        rc = self.lib.nvCreateAprilTagsDetector(
            ctypes.byref(hnd), w, h, self.tile, self.FAMILIES["tag36h11"],
            ctypes.byref(cam), self.tag_size)
        if rc != 0:
            raise RuntimeError(f"nvCreateAprilTagsDetector failed ({rc})")
        self.handle, self.cfg = hnd, cfg

    def detect(self, gray: np.ndarray, K: np.ndarray) -> list[Detection]:
        import cv2

        h, w = gray.shape[:2]
        self._ensure(w, h, K)
        rgb = np.ascontiguousarray(cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB))
        img = _CuImage(self.cuda.upload(rgb), w * 3, w, h)
        n = ctypes.c_uint32(0)
        rc = self.lib.cuAprilTagsDetect(self.handle, ctypes.byref(img), self.out,
                                        ctypes.byref(n), self.max_tags, None)
        if rc != 0:
            raise RuntimeError(f"cuAprilTagsDetect failed ({rc})")
        out = []
        for i in range(n.value):
            t = self.out[i]
            out.append(Detection(
                int(t.id), "tag36h11",
                np.array([(c.x, c.y) for c in t.corners], np.float64),
                int(t.hamming_error),
                # column-major -> R; already this service's convention per the
                # experiment (no 180-degree flip, unlike the CPU library)
                R=np.array(t.orientation, np.float64).reshape(3, 3).T,
                t=np.array(t.translation, np.float64),
                size_m=self.tag_size))
        return out

    def close(self) -> None:
        if self.handle:
            self.lib.cuAprilTagsDestroy(self.handle)
            self.handle, self.cfg = None, None


# --------------------------------------------------------------------------- #
def build(s: Settings) -> Any:
    if s.detector in ("auto", "cuda"):
        try:
            return CuAprilTagsDetector(s)
        except Exception as e:                      # noqa: BLE001
            if s.detector == "cuda":
                raise SystemExit(f"[det] CUDA detector unavailable: {e}") from e
            log.info(f"[det] CUDA detector unavailable ({e}); using CPU")
    return PupilDetector(s)
