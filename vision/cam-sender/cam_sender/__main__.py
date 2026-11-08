#!/usr/bin/env python3
"""
Camera -> JPEG -> ZMQ PUB, in the format vision/april-detect consumes.

    python3 -m cam_sender                         # 1280x720 @ 15 fps, grey, GPU
    python3 -m cam_sender --fps 10 --color
    python3 -m cam_sender --backend test          # no camera
    python3 -m cam_sender --record --rec-control tcp://<jetson>:5580

The detector connects to this host's --bind address (APRIL_SOURCE).

The frame loop below does nothing that can wait on the network or the disk:

  * the send is NOBLOCK on a PUB (wire.py), so a stalled link costs frames on
    the wire, never time here;
  * the local recording (--record) takes each frame into a bounded queue
    (localrec.py); its writer, and the control socket that starts and stops
    it (control.py), are threads of their own;
  * logging is queued too (log.py), because stderr can block -- a dropped SSH
    session, a journald behind a busy SD card;
  * the heartbeat file is touched by its own thread, because even a utime()
    can wait on an ext4 journal commit behind the recording's fsyncs.

A camera that cannot be sent, or recorded, as fast as it produces frames
shows it as `skipped` in the stats line (and a gap in `seq`).
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from typing import Optional

from . import backends, wire
from .config import parse_args
from .control import ControlListener
from .localrec import LocalRecorder
from .log import flush as flush_log
from .log import log


class Heartbeat:
    """Touches a file once a second while frames flow. The Docker healthcheck
    reads its age, so a camera that stops delivering frames makes the
    container unhealthy.

    The frame loop only calls beat(), which stores the time; this thread does
    the file-system calls."""

    FRESH_S = 2.0

    def __init__(self, path: str) -> None:
        self.path = path
        self.last = float("-inf")
        self._halt = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def beat(self) -> None:
        self.last = time.monotonic()

    def start(self) -> None:
        if self.path:
            self._thread = threading.Thread(target=self._run, name="heartbeat",
                                            daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._halt.wait(1.0):
            if time.monotonic() - self.last < self.FRESH_S:
                try:
                    with open(self.path, "a"):
                        pass
                    os.utime(self.path)
                except OSError:
                    pass                           # liveness aid, not fatal

    def stop(self) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(2.0)
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

    def line(self, now: float, be: backends.Backend,
             rec: Optional[LocalRecorder] = None,
             ctl: Optional[ControlListener] = None) -> str:
        dt = max(now - self.t0, 1e-9)
        n = max(self.n, 1)
        s = (f"[stats] {self.n / dt:5.1f} fps  {self.nbytes / n / 1024:6.1f} KB/frame  "
             f"{self.nbytes * 8 / dt / 1e6:5.2f} Mbit/s  skipped {self.skipped}  "
             f"failed {self.failed}")
        if self.n_enc:
            s += f"  enc {self.enc_ms / self.n_enc:5.1f} ms"
        if be.qc is not None and be.qc.target:
            s += f"  q {be.qc.q}"
        s += f"  t_ms: {be.ts_source}"
        if rec is not None:
            a = rec.active
            if a is None:
                s += "  rec idle"
            else:
                s += (f"  rec {a.name} {a.written} written  q "
                      f"{a.queue.nbytes / 1e6:.1f} MB  drop {a.queue.dropped}")
                if a.write_errors:
                    s += f"  errors {a.write_errors}"
            if ctl is not None and ctl.silent_s() > 3.0:
                s += f"  (control silent {ctl.silent_s():.0f} s)"
        return s


def main(argv: Optional[list[str]] = None,
         stop: Optional[threading.Event] = None) -> int:
    """`stop` lets a caller (the tests) end the run; signals do otherwise."""
    args = parse_args(argv)
    stop = stop if stop is not None else threading.Event()

    def on_signal(signum: int, _frame: object) -> None:
        log(f"[main] signal {signum}, stopping")
        stop.set()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGTERM, on_signal)
        # Run from an SSH session, a dropped connection ends in SIGHUP once
        # sshd gives up, and its default action would kill the process -- and
        # the local recording with it, in the very outage it exists for.
        # SIGINT and SIGTERM stop the sender; a hangup does not.
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)

    pub = wire.Publisher(args.bind, args.sndhwm)
    log(f"[net] PUB bind {args.bind} as '{args.name}'")
    be = backends.create(args, stop)
    hb = Heartbeat(args.heartbeat)
    hb.start()
    st = Stats()

    rec: Optional[LocalRecorder] = None
    ctl: Optional[ControlListener] = None
    if args.record:
        rec = LocalRecorder(
            Path(args.record_dir), args.name, now_ns=lambda: be.clock.now_ns(),
            queue_mb=args.rec_queue_mb, min_free_mb=args.rec_min_free_mb,
            max_s=args.rec_max_s, fsync_s=args.rec_fsync_s,
            settings={k: getattr(args, k) for k in (
                "backend", "width", "height", "fps", "color", "target_kb",
                "quality", "hflip", "vflip", "bind")})
        ctl = ControlListener(args.rec_control, rec.want, stop,
                              now_ns=lambda: be.clock.now_ns())
        ctl.start()
        log(f"[rec] local recording to {Path(args.record_dir).resolve()}, "
            f"started and stopped by the recorder at {args.rec_control}")

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
            sess = rec.active if rec is not None else None   # one attribute read
            if sess is not None:
                hdr["rec"] = sess.name          # tells the Jetson this frame is kept here
            parts = wire.stamp_and_pack(hdr, f.jpeg, be.clock.now_ms)
            if pub.send_parts(parts):
                st.n += 1
                st.nbytes += len(f.jpeg)
            else:
                st.failed += 1
            if sess is not None:
                # Whether or not the send went out: a frame the network could
                # not take is the one the local copy is for.
                sess.offer(parts[0], f.jpeg, hdr["t_send_ms"])
            seq = (seq + 1) % (2 ** 32)
            st.skipped += f.skipped
            if f.enc_ms is not None:
                st.enc_ms += f.enc_ms
                st.n_enc += 1
            hb.beat()

            now = time.monotonic()
            if args.stats_interval > 0 and now - st.t0 >= args.stats_interval:
                log(st.line(now, be, rec, ctl))
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
        if ctl is not None:
            ctl.join(2.0)
        if rec is not None:
            rec.shutdown("shutdown")               # drain, fsync, meta.json
        pub.close()
        hb.stop()
    log("[main] done")
    flush_log(2.0)
    return rc


if __name__ == "__main__":
    import sys

    sys.exit(main())
