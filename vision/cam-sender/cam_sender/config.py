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
    CAM_RECORD  CAM_RECORD_DIR  CAM_REC_CONTROL  CAM_REC_QUEUE_MB
    CAM_REC_MIN_FREE_MB  CAM_REC_MAX_S  CAM_REC_FSYNC_S
    CAM_STATS_INTERVAL_S  CAM_HEARTBEAT
"""

from __future__ import annotations

import argparse
import os
import socket
import tempfile
from typing import Any, Callable, Optional

BACKENDS = ("hw", "sw", "uvc", "test")

CONTROL_PORT = 5580
"""The recorder's default REC_CAM_CONTROL_BIND port."""


def control_endpoint(spec: str) -> str:
    """The recorder's control address as a ZMQ endpoint. Takes the full form
    (`tcp://192.168.50.1:5580`) or the short ones people actually type:
    `192.168.50.1`, `192.168.50.1:5580`, `jetson.local`, `tcp://host`. A
    missing port is the recorder's default, 5580. Raises ValueError, so a bad
    address stops startup instead of failing later on the control thread."""
    s = spec.strip()
    scheme, sep, rest = s.partition("://")
    if not sep:
        scheme, rest = "tcp", s
    if scheme != "tcp":
        raise ValueError(f"only tcp:// is supported, not {scheme}://")
    rest = rest.rstrip("/")
    if rest.startswith("["):                    # [IPv6]:port
        host, _, tail = rest.partition("]")
        host += "]"
        if tail and not tail.startswith(":"):
            raise ValueError(f"cannot read {s!r} as host:port")
        port = tail[1:] if tail else ""
    elif rest.count(":") == 1:
        host, port = rest.split(":")
    elif ":" in rest:
        raise ValueError(f"IPv6 addresses need brackets: tcp://[{rest}]:{CONTROL_PORT}")
    else:
        host, port = rest, ""
    if not host or host == "[]" or any(c in host for c in "/ \t"):
        raise ValueError(f"no host in {s!r}")
    port = port or str(CONTROL_PORT)
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"bad port {port!r}")
    return f"tcp://{host}:{port}"


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

    r = p.add_argument_group(
        "local recording",
        "every frame also written to this Pi's own disk, started and stopped "
        "with embedded/recorder's sessions and named as they are, so a "
        "session survives a Wi-Fi outage (README, \"Local recording\")")
    r.add_argument("--record", action=argparse.BooleanOptionalAction,
                   default=_env("RECORD", False, _bool),
                   help="record locally whenever the recorder at --rec-control "
                        "is recording")
    r.add_argument("--record-dir", default=_env("RECORD_DIR", "rec"),
                   help="sessions go here, one directory each")
    r.add_argument("--rec-control", default=_env("REC_CONTROL", ""),
                   help="the recorder's camera control address (its "
                        "REC_CAM_CONTROL_BIND): tcp://<jetson>:5580, or just "
                        "the Jetson's IP. Prefer the IP to a .local name")
    r.add_argument("--rec-queue-mb", type=float,
                   default=_env("REC_QUEUE_MB", 32.0, float),
                   help="frames waiting for the disk; past this the oldest "
                        "waiting frame is dropped from the recording (never "
                        "from the stream) and counted")
    r.add_argument("--rec-min-free-mb", type=float,
                   default=_env("REC_MIN_FREE_MB", 1000.0, float),
                   help="a session will not start, and a running one stops, "
                        "below this much free space")
    r.add_argument("--rec-max-s", type=float,
                   default=_env("REC_MAX_S", 10800.0, float),
                   help="stop a session after this long, in case the "
                        "recorder's stop never arrives; 0: no limit")
    r.add_argument("--rec-fsync-s", type=float,
                   default=_env("REC_FSYNC_S", 5.0, float),
                   help="seconds between fsyncs: the most a power cut can "
                        "cost; 0 disables")

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
    if args.record:
        if not args.rec_control.strip():
            p.error("--record needs --rec-control (CAM_REC_CONTROL): the "
                    "recorder's tcp://<jetson>:5580, which starts and stops "
                    "the local sessions")
        try:
            args.rec_control = control_endpoint(args.rec_control)
        except ValueError as e:
            p.error(f"--rec-control (CAM_REC_CONTROL) {args.rec_control!r}: {e}")
        if not args.record_dir:
            p.error("--record needs --record-dir")
        if args.rec_queue_mb <= 0:
            p.error("--rec-queue-mb must be positive")
        if args.rec_max_s < 0 or args.rec_min_free_mb < 0 or args.rec_fsync_s < 0:
            p.error("--rec-max-s, --rec-min-free-mb and --rec-fsync-s must be >= 0")
    return args
