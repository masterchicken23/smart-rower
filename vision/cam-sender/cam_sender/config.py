#!/usr/bin/env python3
"""
Command-line flags, each defaulting to a CAM_* environment variable.

Flags are the interface on a bare Pi (`python3 -m cam_sender --fps 10`); the
environment is the interface in Docker, where docker-compose and .env are the
configuration surface, as for april-detect's APRIL_* and the funnel's
FUNNEL_*. A flag given on the command line wins over the environment.

    CAM_NAME  CAM_BIND  CAM_SNDHWM
    CAM_BACKEND  CAM_DEVICE  CAM_WIDTH  CAM_HEIGHT  CAM_FPS  CAM_HFLIP  CAM_VFLIP
    CAM_COLOR  CAM_TARGET_KB  CAM_QUALITY  CAM_QMIN  CAM_QMAX
    CAM_STATS_INTERVAL_S  CAM_HEARTBEAT
"""

from __future__ import annotations

import argparse
import os
import socket
import tempfile
from typing import Any, Callable, Optional

BACKENDS = ("hw", "sw", "uvc", "test")


def _bool(s: str) -> bool:
    v = s.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"not a boolean: {s!r}")


def _env(name: str, default: Any, conv: Callable[[str], Any] = str) -> Any:
    raw = os.environ.get("CAM_" + name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return conv(raw)
    except ValueError as e:
        raise SystemExit(f"CAM_{name}={raw!r}: {e}") from None


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cam_sender",
        description="Pi Zero camera -> JPEG -> ZMQ PUB, in the format "
                    "vision/april-detect consumes (see its SENDER.md).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    n = p.add_argument_group("network")
    n.add_argument("--name", default=_env("NAME", socket.gethostname()),
                   help="camera id sent as `cam` in every frame header")
    n.add_argument("--bind", default=_env("BIND", "tcp://*:5555"),
                   help="PUB bind address; the detector connects to it")
    n.add_argument("--sndhwm", type=int, default=_env("SNDHWM", 2, int),
                   help="PUB high-water mark; frames beyond it are dropped, "
                        "never queued")

    c = p.add_argument_group("camera")
    c.add_argument("--backend", choices=BACKENDS, default=_env("BACKEND", "hw"),
                   help="hw: GPU MJPEG encoder (Pi Zero default). sw: CPU JPEG "
                        "via simplejpeg. uvc: USB webcam MJPEG passed through. "
                        "test: synthetic frames, no camera")
    c.add_argument("--device", default=_env("DEVICE", "/dev/video0"),
                   help="uvc: the V4L2 device of the USB webcam")
    c.add_argument("--width", type=int, default=_env("WIDTH", 1280, int))
    c.add_argument("--height", type=int, default=_env("HEIGHT", 720, int))
    c.add_argument("--fps", type=float, default=_env("FPS", 15.0, float))
    c.add_argument("--hflip", action=argparse.BooleanOptionalAction,
                   default=_env("HFLIP", False, _bool))
    c.add_argument("--vflip", action=argparse.BooleanOptionalAction,
                   default=_env("VFLIP", False, _bool))

    j = p.add_argument_group("compression")
    j.add_argument("--color", action=argparse.BooleanOptionalAction,
                   default=_env("COLOR", False, _bool),
                   help="send colour. Off: greyscale, which the detector "
                        "needs no more than and which is smaller and cheaper")
    j.add_argument("--target-kb", type=float, default=_env("TARGET_KB", 35.0, float),
                   help="target KB per frame. hw: sets the encoder bitrate. "
                        "sw/test: servoes --quality toward it. 0: fixed quality "
                        "(sw/test) or the encoder's default bitrate (hw)")
    j.add_argument("--quality", type=int, default=_env("QUALITY", 60, int),
                   help="sw/test: JPEG quality, the start point when servoing")
    j.add_argument("--qmin", type=int, default=_env("QMIN", 15, int))
    j.add_argument("--qmax", type=int, default=_env("QMAX", 92, int))

    m = p.add_argument_group("misc")
    m.add_argument("--stats-interval", type=float,
                   default=_env("STATS_INTERVAL_S", 5.0, float),
                   help="seconds between stderr stats lines; 0 disables")
    m.add_argument("--heartbeat",
                   default=_env("HEARTBEAT",
                                os.path.join(tempfile.gettempdir(),
                                             "cam-sender.alive")),
                   help="file touched while frames flow (Docker healthcheck); "
                        "empty disables")

    args = p.parse_args(argv)
    if args.fps <= 0:
        p.error("--fps must be positive")
    if args.width <= 0 or args.height <= 0 or args.width % 2 or args.height % 2:
        p.error("--width and --height must be positive and even")
    if not 1 <= args.qmin <= args.quality <= args.qmax <= 100:
        p.error("need 1 <= --qmin <= --quality <= --qmax <= 100")
    if args.target_kb < 0:
        p.error("--target-kb must be >= 0")
    return args
