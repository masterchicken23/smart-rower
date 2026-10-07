#!/usr/bin/env python3
"""
Can this machine run N detectors at F fps? Answers it without cameras, network
or Docker, so the result is about compute alone.

Spawns --streams worker processes -- one per container in production -- each
fed synthetic JPEGs at --fps on a fixed schedule, latest-wins exactly like the
real receive path: a frame still being processed when the next is due is
superseded, and counted as skipped. Each worker runs the real
Pipeline.process(), configured from the same APRIL_* environment variables
the container reads, so a setting tested here is the setting deployed.

    python tools/bench.py                          # 4 x 15 fps, 720p, 20 s
    APRIL_DECIMATE=1 python tools/bench.py         # what full-res detection costs
    python tools/bench.py --streams 1 --fps 1000   # one stream flat out: max fps
    python tools/bench.py --distort=-0.3,0.09,0,0,0 --tags 3

Pass = every stream holds its rate with nothing skipped. `cpu/frame` is CPU
time across all of a worker's threads per frame, so `cpu/frame x total fps`
is the core count the whole set needs, independent of how the scheduler
happened to spread it. (On Linux. Windows samples process CPU time on its
15.6 ms tick, so that column is unreliable there; the fps and latency columns
are not.)
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_frames(size: str, n: int, n_tags: int, distort: str, quality: int,
                fx: float, noise: float = 2.0) -> list[bytes]:
    import cv2
    import numpy as np

    from april_detect.calib import Calibration, K_from, parse_floats, parse_size
    from april_detect.synth import render, scene_at

    w, h = parse_size(size)  # type: ignore[misc]
    D = np.array(parse_floats(distort)) if distort else np.zeros(0)
    cal = Calibration(K_from(fx, fx, (w - 1) / 2, (h - 1) / 2), D, size=(w, h))
    frames = []
    for i in range(n):
        tags = []
        for j in range(n_tags):
            tg = scene_at(i + 7 * j, 0.10, tag_id=j)[0]
            tg["t"] = [tg["t"][0] + 0.3 * (j - (n_tags - 1) / 2), tg["t"][1], tg["t"][2]]
            tags.append(tg)
        img = render(cal, tags, noise=noise, seed=i)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        frames.append(jpg.tobytes())
    return frames


def worker(idx: int, frames: list[bytes], fps: float, seconds: float,
           calib_env: dict, start_at: float, q: mp.Queue) -> None:
    os.environ.update(calib_env)
    import json

    import cv2

    from april_detect import log
    from april_detect.config import load
    from april_detect.pipeline import Pipeline

    s = load()
    log.set_level("warn")
    cv2.setNumThreads(s.cv_threads)
    p = Pipeline(s)
    hdr = json.dumps({"msg": {"cam": f"bench{idx}"}}).encode()
    p.process([hdr, frames[0]], time.perf_counter(), time.time())   # warm-up

    period = 1.0 / fps
    while time.time() < start_at:
        time.sleep(0.001)
    t0 = time.perf_counter()
    cpu0 = time.process_time()
    last = -1
    n = skipped = tags = 0
    proc: list[float] = []
    lat: list[float] = []
    while True:
        now = time.perf_counter()
        if now - t0 >= seconds:
            break
        k = int((now - t0) / period)              # newest frame "arrived"
        if k == last:
            time.sleep(max(0.0, t0 + (k + 1) * period - now))
            continue
        skipped += max(0, k - last - 1)
        last = k
        arrived = t0 + k * period
        a = time.perf_counter()
        rec = p.process([hdr, frames[k % len(frames)]], arrived, time.time())
        b = time.perf_counter()
        n += 1
        tags += rec["n"] if rec else 0
        proc.append((b - a) * 1000)
        lat.append((b - arrived) * 1000)          # arrival -> record ready
    cpu = time.process_time() - cpu0
    wall = time.perf_counter() - t0
    q.put({"idx": idx, "n": n, "skipped": skipped, "wall": wall, "cpu": cpu,
           "tags": tags, "proc": proc, "lat": lat,
           "detector": p.det.describe()})


def pct(v: list[float], p: float) -> float:
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))] if v else float("nan")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--streams", type=int, default=4)
    ap.add_argument("--fps", type=float, default=15.0)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--tags", type=int, default=1, help="tags in view per frame")
    ap.add_argument("--distort", default="-0.3,0.09,0,0,0",
                    help="pinhole distortion of the synthetic camera; the "
                         "detectors are configured to undistort it")
    ap.add_argument("--quality", type=int, default=85)
    ap.add_argument("--fx", type=float, default=900.0)
    ap.add_argument("--noise", type=float, default=2.0,
                    help="pixel noise sigma. Matters: detection cost is driven "
                         "by noise more than resolution. 2 is a small sensor in "
                         "shade; use real frames' noise if you know it")
    args = ap.parse_args(argv)

    from april_detect.calib import parse_size

    w, h = parse_size(args.size)  # type: ignore[misc]
    frames = make_frames(args.size, 30, args.tags, args.distort, args.quality,
                         args.fx, args.noise)
    kb = sum(map(len, frames)) / len(frames) / 1024
    calib_env = {
        "APRIL_CAMERA_MATRIX": f"{args.fx} {args.fx} {(w - 1) / 2} {(h - 1) / 2}",
        "APRIL_DIST_COEFFS": args.distort.replace(",", " "),
        "APRIL_CALIB_SIZE": args.size,
        "APRIL_HEARTBEAT_PATH": "",
    }
    # The synthetic frames define the camera, so their calibration always
    # wins over a real one the container happens to carry (CAMN_CALIB):
    # timing would be unaffected, but poses would be nonsense.
    calib_env["APRIL_CALIB_PATH"] = ""
    knobs = {k: v for k, v in os.environ.items()
             if k.startswith("APRIL_") and k not in calib_env}
    print(f"[bench] {args.streams} x {args.fps:g} fps, {args.size}, {kb:.0f} KB/frame, "
          f"{args.tags} tag(s), {os.cpu_count()} cpus, {args.seconds:g}s"
          f"{'  env: ' + ' '.join(f'{k}={v}' for k, v in knobs.items()) if knobs else ''}",
          flush=True)

    q: mp.Queue = mp.Queue()
    start_at = time.time() + 3.0
    procs = [mp.Process(target=worker, args=(i, frames, args.fps, args.seconds,
                                             calib_env, start_at, q))
             for i in range(args.streams)]
    for p in procs:
        p.start()
    res = sorted((q.get() for _ in procs), key=lambda r: r["idx"])
    for p in procs:
        p.join()

    ok = True
    tot_fps = tot_cpu = 0.0
    for r in res:
        fps = r["n"] / r["wall"]
        cpu_ms = 1000 * r["cpu"] / max(r["n"], 1)
        tot_fps += fps
        tot_cpu += r["cpu"] / r["wall"]
        held = r["skipped"] == 0 and fps >= 0.97 * args.fps
        ok &= held
        print(f"  stream {r['idx']}: {fps:5.1f} fps  skipped {r['skipped']:3d}  "
              f"tags/frame {r['tags'] / max(r['n'], 1):.2f}  "
              f"proc p50 {pct(r['proc'], 50):5.1f} p95 {pct(r['proc'], 95):5.1f} "
              f"max {max(r['proc']):5.1f} ms  arrival->out p95 {pct(r['lat'], 95):5.1f} ms  "
              f"cpu/frame {cpu_ms:5.1f} ms  {'ok' if held else 'BEHIND'}")
    print(f"[bench] {res[0]['detector']}: total {tot_fps:.1f} fps using "
          f"{tot_cpu:.2f} of {os.cpu_count()} cores  ->  "
          f"{'PASS' if ok else 'FAIL'}: {args.streams} x {args.fps:g} fps")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
