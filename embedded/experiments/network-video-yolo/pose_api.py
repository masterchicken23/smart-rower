#!/usr/bin/env python3
"""
REST view of the live pose stream. Placeholder for the processing service.

  pose_stream.py --publish tcp://*:5556   ->   ZMQ SUB   ->   HTTP

Subscribes to the PUB socket, keeps only the newest record per source, and
serves it. Nothing is queued or replayed: every request answers "what is true
now", which is what a mobile client polling at its own rate wants.

  GET /keypoints          the documented shape, normalised 0-1
  GET /raw                the full upstream record, unmodified
  GET /health             liveness, data age, sources, counters
  GET /sources            what has been seen publishing
  GET /                   this list

/keypoints returns exactly:

  {"keypoints": [{"name": "nose", "x": 0.51, "y": 0.22, "confidence": 0.98}, ...]}

Keypoints the model could not place -- gated out upstream by --kp-conf, or below
--min-confidence here -- are omitted rather than sent as nulls or guesses, so
every entry in the array is a real measurement.

Coordinates are divided by frame width and height separately. That is right for
drawing on the frame, and wrong for deriving joint angles, because the two axes
scale differently unless the frame is square. /raw carries the pixel values and
the upstream 'rowing' block for anything geometric.

Stdlib only apart from pyzmq -- no web framework to install on the Jetson.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

STOP = threading.Event()


def log(*a: Any) -> None:
    print(*a, file=sys.stderr, flush=True)


class Latest:
    """Newest record per source, plus the meta that names the keypoints."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pose: dict = {}      # src -> (record, monotonic_recv)
        self._meta: dict = {}      # src -> meta record
        self._last_src: Optional[str] = None
        self.n_pose = 0
        self.n_meta = 0

    def put_pose(self, rec: dict) -> None:
        src = str(rec.get("src") or "default")
        with self._lock:
            self._pose[src] = (rec, time.monotonic())
            self._last_src = src
            self.n_pose += 1

    def put_meta(self, rec: dict) -> None:
        src = str(rec.get("source") or "default")
        with self._lock:
            # meta is keyed by the stream's own source address; also stash it
            # under the sender name once pose records reveal it
            self._meta[src] = rec
            self._meta["*"] = rec
            self.n_meta += 1

    def get(self, src: Optional[str]) -> tuple:
        with self._lock:
            key = src or self._last_src
            if key is None or key not in self._pose:
                return None, None, None
            rec, t = self._pose[key]
            meta = self._meta.get(key) or self._meta.get("*")
            return rec, (time.monotonic() - t) * 1000.0, meta

    def sources(self) -> list:
        with self._lock:
            now = time.monotonic()
            return [{"src": k, "age_ms": round((now - t) * 1000.0, 1),
                     "tick": r.get("tick")}
                    for k, (r, t) in sorted(self._pose.items())]


class Subscriber(threading.Thread):
    def __init__(self, addresses: list, latest: Latest) -> None:
        super().__init__(name="sub", daemon=True)
        self.addresses = addresses
        self.latest = latest
        self.connected = False

    def run(self) -> None:
        import zmq

        ctx = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.RCVTIMEO, 500)   # so STOP is noticed promptly
        sock.setsockopt(zmq.LINGER, 0)
        sock.subscribe(b"")
        for a in self.addresses:
            sock.connect(a)
            log(f"[sub] connected {a}")
        self.connected = True
        while not STOP.is_set():
            try:
                topic, payload = sock.recv_multipart()
            except zmq.Again:
                continue
            except Exception as e:  # noqa: BLE001
                if STOP.is_set():
                    break
                log(f"[sub] recv error: {e!r}")
                continue
            try:
                rec = json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
            if topic == b"meta":
                self.latest.put_meta(rec)
            elif topic == b"pose":
                self.latest.put_pose(rec)
        sock.close()
        ctx.term()


def to_keypoints(rec: dict, meta: Optional[dict], min_conf: float) -> list:
    """Upstream record -> the documented keypoints array."""
    if not rec or not meta:
        return []
    people = rec.get("people") or []
    if not people:
        return []                       # live, but nobody detected
    names = meta.get("keypoints") or []
    kp = people[0].get("kp") or []      # person 0 is the highest-confidence one
    w = rec.get("w") or 0
    h = rec.get("h") or 0
    already_norm = bool(meta.get("normalized"))
    out = []
    for i, point in enumerate(kp):
        if i >= len(names) or not point:
            break
        x, y, c = point[0], point[1], point[2]
        if x is None or y is None:      # gated upstream: omit, never guess
            continue
        if c is None or c < min_conf:
            continue
        if not already_norm:
            if not w or not h:
                continue
            x, y = x / w, y / h
        out.append({"name": names[i], "x": round(float(x), 4),
                    "y": round(float(y), 4), "confidence": round(float(c), 3)})
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "pose_api/1.0"
    latest: Latest = None       # type: ignore[assignment]
    args: Any = None
    started = time.time()

    def log_message(self, fmt, *a):     # quiet by default; stderr is for us
        if self.args and self.args.access_log:
            log(f"[http] {self.address_string()} {fmt % a}")

    # -- helpers ------------------------------------------------------------ #
    def _send(self, code: int, body: dict, headers: Optional[dict] = None) -> None:
        raw = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if self.args.cors:
            self.send_header("Access-Control-Allow-Origin", self.args.cors)
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass                        # client hung up mid-response

    def do_OPTIONS(self):               # noqa: N802
        self.send_response(204)
        if self.args.cors:
            self.send_header("Access-Control-Allow-Origin", self.args.cors)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self):                   # noqa: N802
        url = urlparse(self.path)
        q = parse_qs(url.query)
        src = (q.get("src") or [None])[0]
        path = url.path.rstrip("/") or "/"

        if path == "/keypoints":
            return self._keypoints(src)
        if path == "/raw":
            return self._raw(src)
        if path == "/health":
            return self._health()
        if path == "/sources":
            return self._send(200, {"sources": self.latest.sources()})
        if path == "/":
            return self._send(200, {
                "service": "pose_api",
                "endpoints": ["/keypoints", "/raw", "/health", "/sources"],
                "note": "add ?src=NAME to select a stream; /sources lists them",
            })
        return self._send(404, {"error": "not found", "path": url.path})

    # -- endpoints ---------------------------------------------------------- #
    def _keypoints(self, src):
        rec, age_ms, meta = self.latest.get(src)
        hdrs = {}
        if rec is None:
            hdrs["X-Pose-Status"] = "no-data"
            return self._send(503, {"keypoints": []}, hdrs)
        hdrs["X-Pose-Age-Ms"] = round(age_ms, 1)
        hdrs["X-Pose-Tick"] = rec.get("tick", "")
        hdrs["X-Pose-Source"] = rec.get("src", "")
        if meta is None:
            # names arrive with meta, which is resent every couple of seconds
            hdrs["X-Pose-Status"] = "awaiting-meta"
            return self._send(503, {"keypoints": []}, hdrs)
        if self.args.max_age_ms and age_ms > self.args.max_age_ms:
            hdrs["X-Pose-Status"] = "stale"
            return self._send(503, {"keypoints": []}, hdrs)
        hdrs["X-Pose-Status"] = "ok"
        return self._send(200, {"keypoints": to_keypoints(
            rec, meta, self.args.min_confidence)}, hdrs)

    def _raw(self, src):
        rec, age_ms, meta = self.latest.get(src)
        if rec is None:
            return self._send(503, {"error": "no data yet"})
        return self._send(200, {"age_ms": round(age_ms, 1), "meta": meta,
                                "record": rec})

    def _health(self):
        srcs = self.latest.sources()
        fresh = [s for s in srcs
                 if not self.args.max_age_ms or s["age_ms"] <= self.args.max_age_ms]
        ok = bool(fresh)
        return self._send(200 if ok else 503, {
            "status": "ok" if ok else ("stale" if srcs else "no-data"),
            "uptime_s": round(time.time() - self.started, 1),
            "subscribed": self.args.subscribe,
            "sources": srcs,
            "records": self.latest.n_pose,
            "meta_records": self.latest.n_meta,
            "max_age_ms": self.args.max_age_ms,
            "min_confidence": self.args.min_confidence,
        })


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="REST view of the pose stream published by pose_stream.py.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--subscribe", action="append", default=None,
                   help="pose_stream PUB address; repeat for several streams "
                        "(default: tcp://127.0.0.1:5556)")
    p.add_argument("--host", default="0.0.0.0",
                   help="HTTP bind address; 0.0.0.0 exposes it on the LAN, which "
                        "is what a phone needs. There is no authentication.")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--max-age-ms", type=float, default=1000.0,
                   help="older than this and /keypoints answers 503 rather than "
                        "presenting stale positions as current (0 disables)")
    p.add_argument("--min-confidence", type=float, default=0.0,
                   help="drop keypoints below this confidence from /keypoints")
    p.add_argument("--cors", default="*",
                   help="Access-Control-Allow-Origin value; empty string disables")
    p.add_argument("--access-log", action="store_true", help="log every request")
    args = p.parse_args(argv)
    if not args.subscribe:
        args.subscribe = ["tcp://127.0.0.1:5556"]
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    def on_signal(signum, _frame):
        log(f"[main] signal {signum}, stopping")
        STOP.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    latest = Latest()
    Subscriber(args.subscribe, latest).start()

    Handler.latest = latest
    Handler.args = args
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    log(f"[http] listening on http://{args.host}:{args.port}  "
        f"(no authentication; read-only)")
    log(f"[http] GET /keypoints  /raw  /health  /sources")

    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                         daemon=True)
    t.start()
    try:
        while not STOP.wait(0.3):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        httpd.shutdown()
        httpd.server_close()
    log("[main] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

