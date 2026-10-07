#!/usr/bin/env python3
"""
Watch what the detectors publish, the way the funnel will receive it.

One SUB socket connected to every detector given -- the same topology the
README proposes for the funnel -- printing per-camera rate, drops and latency
every few seconds. The column to read is `e2e`: capture to publish, the whole
pipeline's latency for that camera. `deliv` is publish to this process, i.e.
what the funnel adds on top.

    python tools/tap.py tcp://127.0.0.1:5561 tcp://127.0.0.1:5562
    python tools/tap.py tcp://127.0.0.1:5561 --raw            # print records
    python tools/tap.py tcp://127.0.0.1:5561 --truth          # vs fake_camera

--truth compares each pose against april_detect.synth.scene_at(seq % loop),
so it is only meaningful against tools/fake_camera.py with the same --loop and
--tag-size. It is how to check the cuAprilTags backend's pose convention on the
Jetson, where the test suite cannot reach it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pct(v: list[float], p: float) -> float:
    if not v:
        return float("nan")
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("connect", nargs="+", help="detector PUB addresses")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--raw", action="store_true", help="print every record")
    p.add_argument("--meta", action="store_true", help="print meta records")
    p.add_argument("--truth", action="store_true")
    p.add_argument("--loop", type=int, default=30)
    p.add_argument("--tag-size", type=float, default=0.10)
    args = p.parse_args(argv)

    import numpy as np
    import zmq

    from april_detect import synth, wire

    sock = zmq.Context.instance().socket(zmq.SUB)
    sock.setsockopt(zmq.SUBSCRIBE, wire.TOPIC_TAGS)     # prefix: meta too
    sock.setsockopt(zmq.RCVTIMEO, 500)
    for a in args.connect:
        sock.connect(a)
    print(f"[tap] SUB connect {', '.join(args.connect)}", file=sys.stderr)

    acc: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    last_tick: dict[str, int] = {}
    t_stat = time.monotonic()
    while True:
        try:
            topic, body = sock.recv_multipart()
        except zmq.Again:
            topic = None
        except KeyboardInterrupt:
            return 0
        now_wall = time.time()
        if topic == wire.TOPIC_META:
            if args.meta:
                print(body.decode())
        elif topic == wire.TOPIC_TAGS:
            rec = json.loads(body)
            if args.raw:
                print(body.decode())
            cam = rec["cam"]
            a = acc[cam]
            a["n"].append(1)
            if cam in last_tick and rec["tick"] - last_tick[cam] > 1:
                a["lost"].append(rec["tick"] - last_tick[cam] - 1)
            last_tick[cam] = rec["tick"]
            a["skipped"].append(rec["skipped"])
            a["tags"].append(rec["n"])
            for k in ("e2e", "total", "detect", "net", "enc"):
                if rec["ms"].get(k) is not None:
                    a[k].append(rec["ms"][k])
            a["deliv"].append((now_wall - rec["ts"]["pub"]) * 1000.0)
            a["clock"] = [rec["clock"]]
            if args.truth and rec["seq"] is not None:
                for tr in synth.scene_at(rec["seq"] % args.loop, args.tag_size):
                    got = next((t for t in rec["tags"] if t["id"] == tr["id"]), None)
                    if got is None:
                        a["missed"].append(1)
                        continue
                    a["pos_err_mm"].append(1000 * float(np.linalg.norm(
                        np.array(got["pos_m"]) - np.asarray(tr["t"]))))
                    w, x, y, z = got["quat"]
                    Rg = np.array([
                        [1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
                        [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
                        [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)]])
                    c = (np.trace(Rg.T @ tr["R"]) - 1) / 2
                    a["rot_err_deg"].append(math.degrees(math.acos(max(-1, min(1, c)))))

        now = time.monotonic()
        if now - t_stat >= args.interval:
            dt = now - t_stat
            for cam in sorted(acc):
                a = acc[cam]
                line = (f"{cam:>10}  {len(a['n']) / dt:5.1f}/s  lost {sum(a['lost'])}"
                        f"  skipped {sum(a['skipped'])}  tags {np.mean(a['tags'] or [0]):.2f}"
                        f"  | e2e p50 {pct(a['e2e'], 50):5.1f} p95 {pct(a['e2e'], 95):5.1f}"
                        f" max {max(a['e2e'] or [float('nan')]):5.1f}"
                        f"  recv->pub p95 {pct(a['total'], 95):5.1f}"
                        f"  detect {np.mean(a['detect'] or [float('nan')]):5.1f}"
                        f"  net p50 {pct(a['net'], 50):4.1f}  enc p50 {pct(a['enc'], 50):4.1f}"
                        f"  deliv p95 {pct(a['deliv'], 95):4.1f} ms  [{a['clock'][0] if a['clock'] else '-'}]")
                if args.truth:
                    line += (f"  | pos err p95 {pct(a['pos_err_mm'], 95):5.1f} mm"
                             f"  rot err p95 {pct(a['rot_err_deg'], 95):4.1f} deg"
                             f"  missed {sum(a['missed'])}")
                print(line, flush=True)
            acc.clear()
            t_stat = now


if __name__ == "__main__":
    sys.exit(main())
