#!/usr/bin/env python3
"""
A camera with no camera: publishes synthetic frames of a moving tag over
imageZMQ, with the timing header from SENDER.md. Two jobs:

  * the reference implementation of SENDER.md -- see send_loop(), which is
    the part a real sender copies;
  * driving one or more detector containers at a known rate and resolution
    with known ground truth, for the throughput check and for verifying the
    pose convention on a backend the tests cannot reach (cuAprilTags).

Frames are rendered once up front (--loop frames, default 2 s worth) and
replayed, so this costs almost no CPU while running -- important when it
shares the Jetson with the detectors it is loading. Ground truth for frame
`seq` is april_detect.synth.scene_at(seq % loop); tools/tap.py --truth
checks it.

    python tools/fake_camera.py --bind tcp://*:5555 --name cam1
    python tools/fake_camera.py --bind tcp://*:5555 --distort=-0.3,0.09,0,0,0
    python tools/fake_camera.py --print-env      # the APRIL_* calibration to match

Same flag names as the senders in experiments/network-video-yolo/cam-sender
where they overlap (--bind, --name, --fps, --quality, --sndhwm).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from april_detect.calib import (  # noqa: E402
    Calibration,
    K_from,
    parse_floats,
    parse_size,
)
from april_detect.synth import render, scene_at  # noqa: E402

STOP = threading.Event()


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def build_frames(args: argparse.Namespace, cal: Calibration) -> list[bytes]:
    import cv2

    out = []
    t0 = time.perf_counter()
    for i in range(args.loop):
        img = render(cal, scene_at(i, args.tag_size, args.tag_id), noise=args.noise,
                     seed=i)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, args.quality])
        if not ok:
            raise SystemExit("JPEG encode failed")
        out.append(jpg.tobytes())
    kb = sum(map(len, out)) / len(out) / 1024
    log(f"[cam] rendered {len(out)} frames in {time.perf_counter() - t0:.1f}s, "
        f"{kb:.0f} KB/frame -> {kb * 8 * args.fps / 1024:.1f} Mbit/s at {args.fps:g} fps")
    return out


def send_loop(args: argparse.Namespace, frames: list[bytes]) -> None:
    """The SENDER.md contract, in full. A real sender replaces the frame
    source; everything about the header stays as it is here."""
    import imagezmq
    import zmq

    sender = imagezmq.ImageSender(connect_to=args.bind, REQ_REP=False)
    sender.zmq_socket.setsockopt(zmq.SNDHWM, args.sndhwm)  # drop, never queue
    sender.zmq_socket.setsockopt(zmq.LINGER, 0)
    log(f"[net] PUB bind {args.bind} as '{args.name}' at {args.fps:g} fps")

    # One monotonic clock for both instants. perf_counter is CLOCK_MONOTONIC
    # on Linux; time.monotonic() would do there too, but on Windows it ticks
    # at 15.6 ms, which would swamp the numbers being measured.
    clock_ms = lambda: time.perf_counter() * 1000.0  # noqa: E731

    period = 1.0 / args.fps
    next_t = time.perf_counter()
    seq = 0
    n, t_stat = 0, time.perf_counter()
    while not STOP.is_set():
        # Capture instant. A real camera should use the sensor's own
        # timestamp here (start of exposure) if its stack reports one on this
        # same clock; failing that, the moment the frame was dequeued.
        t_ms = clock_ms()
        jpeg = frames[seq % len(frames)]
        header = {"v": 1, "cam": args.name, "seq": seq, "t_ms": round(t_ms, 3)}
        if args.encode_ms:
            time.sleep(args.encode_ms / 1000.0)   # stand-in for a real encoder
        header["t_send_ms"] = round(clock_ms(), 3)  # last thing before send
        sender.send_jpg(header, jpeg)
        seq = (seq + 1) % (2 ** 32)
        n += 1

        next_t += period
        slack = next_t - time.perf_counter()
        if slack > 0:
            STOP.wait(slack)
        else:
            next_t = time.perf_counter()          # overran: do not burst
        now = time.perf_counter()
        if now - t_stat >= 5.0:
            log(f"[stats] {n / (now - t_stat):5.1f} fps  seq {seq}")
            n, t_stat = 0, now
    sender.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bind", default="tcp://*:5555")
    p.add_argument("--name", default="fakecam")
    p.add_argument("--fps", type=float, default=15.0)
    p.add_argument("--size", default="1280x720")
    p.add_argument("--quality", type=int, default=85)
    p.add_argument("--sndhwm", type=int, default=2)
    p.add_argument("--fx", type=float, default=900.0,
                   help="focal length px at --size (fy = fx, centred)")
    p.add_argument("--distort", default="",
                   help="distortion coefficients, comma-separated (pinhole model)")
    p.add_argument("--tag-id", type=int, default=0)
    p.add_argument("--tag-size", type=float, default=0.10)
    p.add_argument("--loop", type=int, default=30, help="frames rendered and replayed")
    p.add_argument("--noise", type=float, default=2.0, help="pixel noise sigma")
    p.add_argument("--encode-ms", type=float, default=0.0,
                   help="simulated encode delay between capture and send")
    p.add_argument("--print-env", action="store_true",
                   help="print the APRIL_* settings matching this camera and exit")
    args = p.parse_args(argv)

    w, h = parse_size(args.size)  # type: ignore[misc]
    D = np.array(parse_floats(args.distort)) if args.distort else np.zeros(0)
    cal = Calibration(K_from(args.fx, args.fx, (w - 1) / 2, (h - 1) / 2), D,
                      size=(w, h))
    if args.print_env:
        print(f"APRIL_CAMERA_MATRIX={args.fx:g} {args.fx:g} {(w - 1) / 2:g} {(h - 1) / 2:g}")
        print(f"APRIL_DIST_COEFFS={' '.join(f'{v:g}' for v in D)}")
        print(f"APRIL_CALIB_SIZE={w}x{h}")
        print(f"APRIL_TAG_SIZE_M={args.tag_size:g}")
        return 0

    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    send_loop(args, build_frames(args, cal))
    return 0


if __name__ == "__main__":
    sys.exit(main())
