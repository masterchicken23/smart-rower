#!/usr/bin/env python3
"""
Play a video file onto the wire as if it were the Pi camera. For system testing.

Same wire format, same JPEG compression controls and same PUB drop semantics as
pi_sender.py -- it imports them from that script rather than reimplementing, so a
test run exercises the real sender's behaviour. Only the frame source differs.

  video file -> real-time pacing -> JPEG encode -> imagezmq PUB (bind)

Real-time means paced on the wall clock against each frame's presentation
timestamp, not a fixed sleep per frame:

  * Frames are paced by PTS, so variable-frame-rate recordings (most phone MOVs)
    play at their true speed instead of an averaged one.
  * If decoding falls behind -- a 4K MOV on a laptop, or --speed 4 -- frames are
    SKIPPED to stay on the clock rather than played late. That matches what the
    camera path does under load, and keeps the receiver's view of arrival timing
    honest. The count is reported.

MOV notes: QuickTime/iPhone recordings carry a rotation in their display matrix.
OpenCV applies it automatically; --orientation none disables that if your file is
tagged wrongly. The detected rotation is logged at startup, because a sideways
frame will quietly wreck pose estimation.

Requires: opencv-python, imagezmq, pyzmq, numpy, and pi_sender.py alongside.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from pi_sender import (STOP, QualityController, log, make_encoder,
                           make_sender, preflight, target_bytes_from)
except ImportError as e:  # noqa: BLE001
    raise SystemExit(f"cannot import pi_sender.py: {e}\n"
                     "Keep file_sender.py next to pi_sender.py -- it reuses its "
                     "compression and network code so the test path matches the "
                     "real one.")


def open_video(args):
    import cv2

    if not os.path.exists(args.video):
        raise SystemExit(f"no such file: {args.video}")
    cap = cv2.VideoCapture(args.video, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit(
            f"OpenCV could not open {args.video}\n"
            "For .mov this usually means the codec is missing from your OpenCV\n"
            "build -- ProRes and some HEVC files need a fuller FFMPEG. Check:\n"
            f"  ffprobe -v error -show_streams -select_streams v {args.video}\n"
            "and if needed transcode first:\n"
            f"  ffmpeg -i {args.video} -c:v libx264 -pix_fmt yuv420p out.mp4")

    if args.orientation == "none":
        try:
            cap.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
        except Exception:  # noqa: BLE001
            log("[video] this OpenCV cannot disable auto-orientation")

    rot = 0.0
    try:
        rot = cap.get(cv2.CAP_PROP_ORIENTATION_META) or 0.0
    except Exception:  # noqa: BLE001
        pass

    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    dur = n / fps if fps > 0 and n > 0 else 0.0
    log(f"[video] {os.path.basename(args.video)}  {w}x{h}  {fps:.3f} fps  "
        f"{n} frames  {dur:.1f}s")
    if rot:
        log(f"[video] rotation metadata: {rot:g} deg "
            f"({'applied by OpenCV' if args.orientation == 'auto' else 'IGNORED per --orientation none'})")
    if args.start > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, args.start * 1000.0)
        log(f"[video] seeking to {args.start:.2f}s")
    return cap, fps, w, h


def frames(args, qc: QualityController, cap, native_fps, w, h):
    """Yield (jpeg, encode_ms, raw_bytes), paced against the wall clock."""
    import cv2

    encode = make_encoder(args)
    resize = bool(args.width and args.height and (args.width, args.height) != (w, h))
    if resize:
        log(f"[video] rescaling {w}x{h} -> {args.width}x{args.height}")

    rate = args.fps or native_fps or 30.0
    if not native_fps:
        log(f"[video] no fps in the container; pacing at {rate:g}")
    period = 1.0 / rate

    t_origin = time.monotonic()
    pts_origin: Optional[float] = None
    idx = 0
    loops = 0
    stats = {"sent": 0, "dropped": 0, "bytes": 0, "enc_ms": 0.0, "raw": 0,
             "t": time.monotonic(), "late_ms": 0.0}

    while not STOP.is_set():
        if not cap.grab():
            if args.loop:
                loops += 1
                if args.loops and loops >= args.loops:
                    log(f"[video] {loops} loops done")
                    return
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                if args.start > 0:
                    cap.set(cv2.CAP_PROP_POS_MSEC, args.start * 1000.0)
                # restart the clock so the seam is not treated as being late
                t_origin = time.monotonic()
                pts_origin = None
                idx = 0
                log(f"[video] loop {loops}")
                continue
            log("[video] end of file")
            return

        # PTS keeps variable-frame-rate files honest; fall back to a fixed step
        pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if not pts or pts <= 0:
            pts = idx * period
        if pts_origin is None:
            pts_origin = pts
        idx += 1

        target = t_origin + (pts - pts_origin) / args.speed
        now = time.monotonic()

        if args.realtime:
            behind = now - target
            if behind > args.drop_after * period:
                # decoding cannot keep up: skip the retrieve/convert and the send
                # rather than emitting a frame that is already stale
                stats["dropped"] += 1
                continue
            if behind < 0:
                if STOP.wait(-behind):
                    return
            else:
                stats["late_ms"] = max(stats["late_ms"], behind * 1000.0)

        ok, frame = cap.retrieve()
        if not ok or frame is None:
            continue
        if resize:
            frame = cv2.resize(frame, (args.width, args.height),
                               interpolation=cv2.INTER_AREA)

        t_enc = time.monotonic()
        data = encode(frame, qc.q)
        enc_ms = (time.monotonic() - t_enc) * 1000.0

        if args.duration and (pts - pts_origin) >= args.duration and not args.loop:
            log(f"[video] --duration {args.duration}s reached")
            return

        yield data, enc_ms, frame.nbytes, stats

        if args.stats_interval:
            dt = time.monotonic() - stats["t"]
            if dt >= args.stats_interval:
                pos = pts
                log(f"[stats] {stats['sent']/dt:5.1f} fps  "
                    f"{stats['bytes']/max(stats['sent'],1)/1024:6.1f} KB/frame  "
                    f"{stats['raw']/max(stats['bytes'],1):5.1f}:1  q={qc.q:3d}  "
                    f"enc {stats['enc_ms']/max(stats['sent'],1):5.1f} ms  "
                    f"drop {stats['dropped']:4d}  late_max {stats['late_ms']:5.1f} ms  "
                    f"t={pos:6.1f}s")
                stats.update(sent=0, dropped=0, bytes=0, enc_ms=0.0, raw=0,
                             late_ms=0.0, t=time.monotonic())


def main(argv=None) -> int:
    args = parse_args(argv)
    preflight()

    import cv2

    cv2.setNumThreads(args.cv_threads)

    def on_signal(signum, _frame):
        log(f"[main] signal {signum}, stopping")
        STOP.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    cap, native_fps, vw, vh = open_video(args)
    # size the compression budget from the frames we will actually send
    if not (args.width and args.height):
        args.width, args.height = vw, vh

    target = target_bytes_from(args)
    qc = QualityController(args.quality, target, args.qmin, args.qmax)
    if target:
        raw = args.width * args.height * 3
        log(f"[enc] target {target/1024:.1f} KB/frame ({raw/target:.1f}:1), "
            f"quality servoed in [{args.qmin},{args.qmax}], start {qc.q}")
    else:
        log(f"[enc] fixed quality {qc.q}")

    sender = make_sender(args)
    if args.wait > 0:
        # ZMQ PUB drops anything published before a subscriber's subscription has
        # propagated ("slow joiner"), so a run started at the same moment as its
        # receiver loses its first frames. Costless here, and makes frame counts
        # reproducible across test runs.
        log(f"[net] waiting {args.wait:g}s for subscribers to attach")
        if STOP.wait(args.wait):
            return 0
    rc = 0
    try:
        for data, enc_ms, raw, stats in frames(args, qc, cap, native_fps, vw, vh):
            if STOP.is_set():
                break
            qc.update(len(data))
            try:
                sender.send_jpg(args.name, data)
            except Exception as e:  # noqa: BLE001
                if STOP.is_set():
                    break
                log(f"[net] send failed: {e!r}")
                if STOP.wait(0.2):
                    break
                continue
            stats["sent"] += 1
            stats["bytes"] += len(data)
            stats["raw"] += raw
            stats["enc_ms"] += enc_ms
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        if not STOP.is_set():
            log(f"[video] failed: {e}")
            rc = 1
    finally:
        STOP.set()
        try:
            sender.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            cap.release()
        except Exception:  # noqa: BLE001
            pass
    log("[main] done")
    return rc


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Play a video file onto the wire as if it were the Pi camera.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    v = p.add_argument_group("source")
    v.add_argument("--video", required=True, help="path to .mov/.mp4/.avi/...")
    v.add_argument("--loop", action="store_true", help="restart at the end")
    v.add_argument("--loops", type=int, default=0,
                   help="stop after this many loops (0 = forever)")
    v.add_argument("--start", type=float, default=0.0, help="seek to this second")
    v.add_argument("--duration", type=float, default=0.0,
                   help="stop after this many seconds of video (0 = to the end)")
    v.add_argument("--speed", type=float, default=1.0,
                   help="playback rate multiplier; 2.0 plays twice as fast")
    v.add_argument("--fps", type=float, default=0.0,
                   help="override the container's frame rate (0 = use the file's)")
    v.add_argument("--realtime", action="store_true", default=True,
                   help="pace against the wall clock")
    v.add_argument("--no-realtime", dest="realtime", action="store_false",
                   help="push frames as fast as they decode (throughput testing)")
    v.add_argument("--drop-after", type=float, default=1.5,
                   help="skip a frame once it is this many frame-intervals late")
    v.add_argument("--orientation", choices=("auto", "none"), default="auto",
                   help="apply the container's rotation metadata; MOV files from "
                        "phones are usually tagged, and ignoring it gives you a "
                        "sideways frame")
    v.add_argument("--width", type=int, default=0,
                   help="rescale width (0 = native); set both to match the Pi")
    v.add_argument("--height", type=int, default=0, help="rescale height (0 = native)")

    n = p.add_argument_group("network")
    n.add_argument("--bind", default="tcp://*:5555")
    n.add_argument("--connect", default="tcp://127.0.0.1:5555")
    n.add_argument("--mode", choices=("pubsub", "reqrep"), default="pubsub")
    n.add_argument("--name", default=socket.gethostname())
    n.add_argument("--sndhwm", type=int, default=2)
    n.add_argument("--reply-timeout-ms", type=int, default=2000)
    n.add_argument("--wait", type=float, default=0.5,
                   help="pause after binding before the first frame, so a "
                        "subscriber that starts alongside does not miss the "
                        "opening frames (PUB slow joiner). 0 disables.")

    j = p.add_argument_group("compression")
    j.add_argument("--quality", type=int, default=75)
    j.add_argument("--ratio", type=float, default=0.0)
    j.add_argument("--target-kb", type=float, default=0.0)
    j.add_argument("--qmin", type=int, default=15)
    j.add_argument("--qmax", type=int, default=92)
    j.add_argument("--subsampling", choices=("420", "422", "444"), default="420")

    m = p.add_argument_group("misc")
    m.add_argument("--cv-threads", type=int, default=0,
                   help="OpenCV threads; 0 lets OpenCV decide (decoding is the "
                        "work here, unlike on the Pi)")
    m.add_argument("--stats-interval", type=float, default=5.0)
    args = p.parse_args(argv)
    if args.ratio and args.target_kb:
        p.error("--ratio and --target-kb are mutually exclusive")
    if args.speed <= 0:
        p.error("--speed must be positive")
    if bool(args.width) != bool(args.height):
        p.error("set both --width and --height, or neither")
    return args


if __name__ == "__main__":
    sys.exit(main())

