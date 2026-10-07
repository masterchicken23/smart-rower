#!/usr/bin/env python3
"""
Configuration. Every setting is an environment variable prefixed APRIL_, for
the same reason the funnel uses FUNNEL_: docker-compose is the configuration
surface for this service, and one container runs per camera, so the per-camera
differences (source address, output port, calibration) belong in the compose
file next to each service. Names carry their units, as in the rest of the repo.

`python -c "from april_detect.config import load; print(load())"` prints the
effective settings.
"""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CUDA_FAMILIES = ("tag36h11",)
"""cuAprilTags decodes this family and no other."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APRIL_", env_file=".env", extra="ignore"
    )

    # -- identity ----------------------------------------------------------- #
    camera_id: str = ""
    """Name this detector reports as `cam`. Empty: use the sender's own name
    from the frame header, falling back to "cam"."""

    # -- input: frames from the camera (imageZMQ PUB/SUB) ------------------- #
    source: str = "tcp://127.0.0.1:5555"
    """The camera sender's PUB address. In imageZMQ PUB/SUB the sender binds
    and the receiver connects, so this is the camera's address."""
    source_topic: str = ""
    """ZMQ subscription prefix. imageZMQ sends no topic, so leave it empty."""
    recv_hwm: int = 2
    """Receive high-water mark. Small on purpose: frames we cannot keep up with
    should be dropped in the network layer, not queued to be processed late."""
    recv_timeout_ms: int = 250
    no_frames_warn_s: float = 3.0

    # -- output: detections to the funnel (ZMQ PUB) ------------------------- #
    out_address: str = "tcp://*:5557"
    out_bind: bool = True
    """Bind (the funnel connects; the contract in funnel/sources/zmq_pose.py)
    or connect (the funnel binds one SUB that every detector connects to)."""
    out_hwm: int = 16
    meta_period_s: float = 2.0
    emit_empty: bool = True
    """Publish a record for frames with no tags in view. On by default, so the
    funnel can tell "no tag visible" from "detector not running"."""

    # -- tags --------------------------------------------------------------- #
    families: str = "tag36h11"
    """Comma-separated. The CPU detector takes any AprilTag family; the CUDA
    detector only tag36h11."""
    tag_size_m: float = 0.10
    """Edge length of the tag's black square, metres."""
    tag_sizes: str = ""
    """Per-id overrides, `id:metres` comma-separated, e.g. "3:0.05,7:0.20"."""
    tag_ids: str = ""
    """Allow-list, comma-separated. Empty: report every decoded id."""
    max_hamming: int = 1
    """Reject decodes that needed more than this many bit corrections. 0 is
    strictest; 2 doubles the range at which a false positive becomes likely."""

    # -- detector ----------------------------------------------------------- #
    detector: Literal["auto", "cpu", "cuda"] = "auto"
    cuapriltags_lib: str = "/opt/cuapriltags/libcuapriltags.so"
    threads: int = 2
    """CPU detector threads, per container. Four containers x 2 threads leaves
    the Orin Nano's other cores for decode, remap and everything else."""
    decimate: float = 2.0
    """CPU quad_decimate. 2 halves the resolution quads are *found* at (corners
    are still refined and bits still decoded at full resolution): roughly 3x
    faster, at the cost of the smallest detectable tag doubling in pixels."""
    sigma: float = 0.8
    """CPU quad_sigma: Gaussian blur before quads are found. Detection cost is
    driven by sensor noise far more than by resolution -- noise fragments the
    thresholded image into thousands of tiny segments to cluster -- and 0.8
    measured 20 ms -> 5 ms on a noise-sigma-2 720p frame at decimate 2, for
    ~1 ms extra on a clean one. 0 disables."""
    refine_edges: bool = True
    decode_sharpening: float = 0.25
    pose_source: Literal["ippe", "detector"] = "ippe"
    """ippe: solve pose from corners with OpenCV (per-id tag sizes, ambiguity
    reported). detector: use the detector's own pose estimate."""

    # -- decode ------------------------------------------------------------- #
    decode_scale: Literal[1, 2, 4, 8] = 1
    """Decode the JPEG at 1/N size via libjpeg's DCT scaling. Far cheaper than
    decoding at full size and resizing; intrinsics are scaled to match."""

    # -- camera model ------------------------------------------------------- #
    calib_path: str = ""
    """ROS camera_info .yaml/.json, or .npz. Takes precedence over inline."""
    camera_matrix: str = ""
    """Inline: "fx fy cx cy" or 9 row-major values."""
    dist_coeffs: str = ""
    dist_model: str = "pinhole"
    """pinhole (plumb_bob / rational_polynomial) or fisheye (equidistant)."""
    calib_size: str = ""
    """Resolution the inline calibration was made at, e.g. 1280x720."""
    undistort: Literal["image", "points", "none"] = "image"
    undistort_alpha: float = -1.0
    """image mode: <0 keeps K for the corrected image; 0..1 trades cropping for
    field of view (0 = valid pixels only, 1 = every source pixel)."""
    hfov_deg: float = 70.0
    """Only used when no calibration is configured at all."""

    # -- misc --------------------------------------------------------------- #
    cv_threads: int = 1
    """OpenCV's own thread pool (remap, decode helpers). 1 per container: at
    720p grey the parallel speed-up is small and four containers each sizing a
    pool to every core would oversubscribe the CPU."""
    stats_interval_s: float = 5.0
    heartbeat_path: str = "/tmp/april-detect.alive"
    """Touched by the main loop; the container healthcheck reads its age."""
    log_level: str = "info"

    @field_validator("families")
    @classmethod
    def _families(cls, v: str) -> str:
        fams = [f.strip() for f in v.split(",") if f.strip()]
        if not fams:
            raise ValueError("at least one tag family is required")
        return ",".join(fams)

    @property
    def family_list(self) -> list[str]:
        return self.families.split(",")

    @property
    def size_by_id(self) -> dict[int, float]:
        return parse_id_map(self.tag_sizes)

    @property
    def id_allow(self) -> set[int]:
        return {int(t) for t in self.tag_ids.replace(",", " ").split()}

    def size_for(self, tag_id: int) -> float:
        return self.size_by_id.get(tag_id, self.tag_size_m)


def parse_id_map(text: str) -> dict[int, float]:
    """"3:0.05, 7:0.2" -> {3: 0.05, 7: 0.2}."""
    out: dict[int, float] = {}
    for item in text.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        k, v = item.split(":")
        out[int(k)] = float(v)
    return out


def load() -> Settings:
    return Settings()
