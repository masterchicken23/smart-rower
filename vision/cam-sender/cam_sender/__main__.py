#!/usr/bin/env python3
"""
Camera -> JPEG -> ZMQ PUB, in the format vision/april-detect consumes.

    python3 -m cam_sender                         # 1280x720 @ 15 fps, grey, GPU
    python3 -m cam_sender --fps 10 --color
    python3 -m cam_sender --backend test          # no camera

The detector connects to this host's --bind address (APRIL_SOURCE).
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from typing import Optional

from . import backends, wire
from .config import parse_args


def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


class Heartbeat:
    """Touches a file at most once a second while frames flow. The Docker
    healthcheck reads its age, so a camera that stops delivering frames
    makes the container unhealthy."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.last = 0.0

    def touch(self) -> None:
        now = time.monotonic()
        if not self.path or now - self.last < 1.0:
            return
        self.last = now
        try:
            with open(self.path, "a"):
                pass
            os.utime(self.path)
        except OSError:
            pass                                   # liveness aid, not fatal

    def remove(self) -> None:
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass


class Stats:
    def __init__(self) -> None:
        self.reset(time.monotonic())

    def reset(self, now: float) -> None:
        self.t0 = now
        self.n = self.nbytes = self.skipped = self.failed = 0
        self.enc_ms = 0.0
        self.n_enc = 0

    def line(self, now: float, be: backends.Backend) -> str:
        dt = max(now - self.t0, 1e-9)
        n = max(self.n, 1)
        s = (f"[stats] {self.n / dt:5.1f} fps  {self.nbytes / n / 1024:6.1f} KB/frame  "
             f"{self.nbytes * 8 / dt / 1e6:5.2f} Mbit/s  skipped {self.skipped}  "
             f"failed {self.failed}")
        if self.n_enc:
            s += f"  enc {self.enc_ms / self.n_enc:5.1f} ms"
        if be.qc is not None and be.qc.target:
            s += f"  q {be.qc.q}"
        return s + f"  t_ms: {be.ts_source}"


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    stop = threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        log(f"[main] signal {signum}, stopping")
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    pub = wire.Publisher(args.bind, args.sndhwm)
    log(f"[net] PUB bind {args.bind} as '{args.name}'")
    be = backends.create(args, stop)
    hb = Heartbeat(args.heartbeat)
    st = Stats()

    seq = 0
    rc = 0
    frames = be.frames()
    try:
        for f in frames:
            if stop.is_set():
                break
            # Every frame the camera produced gets a seq, sent or not
            # (SENDER.md), so skipped frames leave a countable gap.
            seq = (seq + f.skipped) % (2 ** 32)
            hdr = wire.make_header(args.name, seq, f.t_ms,
                                   w=args.width, h=args.height, **f.extra)
            if pub.send(hdr, f.jpeg, be.clock.now_ms):
                st.n += 1
                st.nbytes += len(f.jpeg)
            else:
                st.failed += 1
            seq = (seq + 1) % (2 ** 32)
            st.skipped += f.skipped
            if f.enc_ms is not None:
                st.enc_ms += f.enc_ms
                st.n_enc += 1
            hb.touch()

            now = time.monotonic()
            if args.stats_interval > 0 and now - st.t0 >= args.stats_interval:
                log(st.line(now, be))
                st.reset(now)
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001 -- a camera fault must exit non-zero
        if not stop.is_set():
            log(f"[cam] stream failed: {type(e).__name__}: {e}")
            rc = 1
    finally:
        stop.set()
        frames.close()                             # releases the camera
        pub.close()
        hb.remove()
    log("[main] done")
    return rc


if __name__ == "__main__":
    sys.exit(main())
