#!/usr/bin/env python3
"""
What is in a session, what was lost, and how well its streams line up.

    python -m recorder.report data/20261107T143000Z
    python -m recorder.report data/<replayed> --compare data/<original>
    python -m recorder.report data/<session> --json > report.json

Sections:

  * MQTT, per topic: messages, rate, sequence gaps (lost between sensor and
    Jetson), sender restarts, retained and non-JSON counts.
  * Cameras: frames, fps, sequence gaps, size, and whether frames came
    through april-detect's tee (with the tee-to-recorder hop's delay).
  * AprilTag, per camera: records, rate, `tick` gaps, meta records.
  * Recorder: queue drops and write errors from meta.json, and steps in the
    wall clock seen during the session.
  * Sync: per device, the clock mapping from sync.py -- segments (reboots),
    drift, and transport delay above the fastest message.

`--compare` matches each device's messages by sequence number against a
second recording of the same data -- the recording of a replay -- and reports
how far the streams moved relative to each other. That is the end-to-end
check that recording and replay preserve alignment.

Argparse, as the repo's other hand-run tools.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from typing import Any, Optional

from .format import (
    INDEX_EXT,
    CorruptFrameError,
    Session,
    frame_header,
    iter_jsonl,
    json_obj,
    num,
)
from .sync import WINDOW_S, DeviceFit, compare, fit_session, pct
from .taps import Device

WALL_STEP_MS = 10.0
"""A jump in `tw - tm` between consecutive messages larger than this is a
wall-clock step (NTP, a manual date), not slewing."""


def _rate(first: Optional[int], last: Optional[int], n: int) -> Optional[float]:
    if first is None or last is None or n < 2 or last <= first:
        return None
    return (n - 1) / ((last - first) / 1e9)


class _Acc:
    def __init__(self) -> None:
        self.dev = Device()
        self.first: Optional[int] = None
        self.extra: dict[str, Any] = {}

    def add(self, tm: int, nbytes: int, seq: Optional[int]) -> None:
        if self.first is None:
            self.first = tm
        self.dev.seen(tm, nbytes, seq)

    def row(self) -> dict[str, Any]:
        d = self.dev
        r = _rate(self.first, d.last_tm, d.n)
        return {"n": d.n, "rate_hz": None if r is None else round(r, 2),
                "gaps": d.n_gap, "restarts": d.n_restart, "bytes": d.nbytes,
                **self.extra}


def _seq(v: Any) -> Optional[int]:
    f = num(v)
    return None if f is None else int(f) % 2 ** 32


def summarize(sess: Session, window_s: float = WINDOW_S, verify: bool = False
              ) -> tuple[dict[str, Any], dict[str, DeviceFit]]:
    """The report as a dict, and the clock fits it was built from. `verify`
    reads every frame and checks its CRC-32 (the whole video, so off by
    default)."""
    first_tm: Optional[int] = None
    last_tm: Optional[int] = None
    wall_steps: list[float] = []
    prev_d: Optional[float] = None

    def clock(rec: dict) -> None:
        nonlocal first_tm, last_tm
        tm = rec["tm"]
        first_tm = tm if first_tm is None else min(first_tm, tm)
        last_tm = tm if last_tm is None else max(last_tm, tm)

    def walls(events: Iterator) -> None:
        nonlocal prev_d
        for ev in events:
            tw = num(ev.rec.get("tw"))
            if tw is None:
                continue
            d = tw - ev.tm / 1e9
            if prev_d is not None and abs(d - prev_d) * 1000.0 > WALL_STEP_MS:
                wall_steps.append(round((d - prev_d) * 1000.0, 1))
            prev_d = d

    mqtt: dict[str, _Acc] = {}
    for ev in sess.mqtt():
        clock(ev.rec)
        p = ev.payload
        obj = json_obj(p)
        a = mqtt.get(ev.source) or mqtt.setdefault(ev.source, _Acc())
        a.add(ev.tm, len(p), _seq(obj.get("seq")) if obj else None)
        if ev.rec.get("retain"):
            a.extra["retained"] = a.extra.get("retained", 0) + 1
        if obj is None:
            a.extra["not_json"] = a.extra.get("not_json", 0) + 1

    cams: dict[str, _Acc] = {}
    for cam in sess.cameras():
        a = cams[cam] = _Acc()
        hop: list[float] = []
        crc_bad = no_crc = 0
        for ev in sess.frames(cam):
            clock(ev.rec)
            h = frame_header(ev.pre)
            a.add(ev.tm, ev.rec["len"], _seq(h.get("seq")))
            if ev.tm_arrival != ev.tm:
                hop.append((ev.tm - ev.tm_arrival) / 1e6)
            if "crc" not in ev.rec:
                no_crc += 1
            elif verify:
                try:
                    sess.jpeg(ev)
                except CorruptFrameError:
                    crc_bad += 1
        # Index lines whose frame is not in the .mjpeg: a cut-off tail, or a
        # damaged line. (Lines too damaged to parse are not even counted.)
        lines = sum(1 for _ in iter_jsonl(sess.path / (cam + INDEX_EXT)))
        a.extra["missing"] = lines - a.dev.n
        a.extra["crc_bad"] = crc_bad if verify and a.dev.n > no_crc else None
        a.extra["tee"] = bool(hop)
        if hop:
            a.extra["tee_hop_ms"] = {"p50": round(pct(hop, 50) or 0, 3),
                                     "p95": round(pct(hop, 95) or 0, 3)}

    april: dict[str, _Acc] = {}
    for ev in sess.apriltag():
        clock(ev.rec)
        rec = json_obj(ev.payload) or {}
        cam = str(rec.get("cam"))
        a = april.get(cam) or april.setdefault(cam, _Acc())
        if ev.source == "apriltag":
            a.add(ev.tm, len(ev.payload), _seq(rec.get("tick")))
            a.extra["tags"] = a.extra.get("tags", 0) + int(num(rec.get("n")) or 0)
        else:
            a.extra["meta"] = a.extra.get("meta", 0) + 1

    walls(sess.events())

    fits = fit_session(sess, window_s)
    sync = {}
    for dev, f in sorted(fits.items()):
        sync[dev] = {
            "kind": f.kind,
            "anchor": f.anchor,
            "n": len(f.capture_s),
            "segments": f.segments,
            "drift_ppm": None if f.drift_ppm is None else round(f.drift_ppm, 1),
            "latency_ms": {k: round(v, 2) for k, v in (
                ("p50", pct(f.latency_ms, 50)), ("p95", pct(f.latency_ms, 95)),
                ("max", max(f.latency_ms) if f.latency_ms else None))
                if v is not None},
        }

    m = sess.meta
    size = sum(p.stat().st_size for p in sess.path.iterdir() if p.is_file())
    summ = m.get("summary") or {}
    return {
        "session": sess.name,
        "complete": bool(m.get("complete")),
        "started": m.get("started"),
        "stop_reason": m.get("stop_reason"),
        "span_s": (None if first_tm is None or last_tm is None
                   else round((last_tm - first_tm) / 1e9, 3)),
        "size_mb": round(size / 1e6, 2),
        "mqtt": {k: a.row() for k, a in sorted(mqtt.items())},
        "cameras": {k: a.row() for k, a in sorted(cams.items())},
        "apriltag": {k: a.row() for k, a in sorted(april.items())},
        "recorder": {"dropped": summ.get("dropped"),
                     "write_errors": summ.get("write_errors"),
                     "wall_clock_steps_ms": wall_steps},
        "sync": sync,
    }, fits


# ---- printing ----------------------------------------------------------- #

def _f(v: Any, fmt: str = "{:.1f}", dash: str = "-") -> str:
    return dash if v is None else fmt.format(v)


def render(r: dict[str, Any]) -> str:
    out = []
    w = out.append
    w(f"session  {r['session']}  "
      f"({'complete' if r['complete'] else 'INCOMPLETE: not stopped cleanly'})")
    w(f"started  {r['started'] or '?'}   span {_f(r['span_s'])} s   "
      f"{r['size_mb']:.1f} MB   stop: {r['stop_reason'] or '-'}")

    if r["mqtt"]:
        w("\nMQTT")
        w(f"  {'topic':<28} {'n':>7} {'rate Hz':>8} {'gaps':>6} {'restart':>7}  notes")
        for t, x in r["mqtt"].items():
            notes = []
            if x.get("retained"):
                notes.append(f"retained {x['retained']}")
            if x.get("not_json"):
                notes.append(f"not JSON {x['not_json']}")
            w(f"  {t:<28} {x['n']:>7} {_f(x['rate_hz']):>8} {x['gaps']:>6} "
              f"{x['restarts']:>7}  {', '.join(notes)}")

    if r["cameras"]:
        w("\ncameras")
        w(f"  {'cam':<8} {'frames':>7} {'fps':>6} {'gaps':>6} {'KB/fr':>7} "
          f"{'MB':>8} {'missing':>7} {'crc bad':>7}  via")
        for c, x in r["cameras"].items():
            kb = x["bytes"] / max(x["n"], 1) / 1024
            via = "direct"
            if x.get("tee"):
                hop = x.get("tee_hop_ms", {})
                via = (f"tee (hop p50 {hop.get('p50', 0):.2f} "
                       f"p95 {hop.get('p95', 0):.2f} ms)")
            crc = "-" if x.get("crc_bad") is None else str(x["crc_bad"])
            w(f"  {c:<8} {x['n']:>7} {_f(x['rate_hz']):>6} {x['gaps']:>6} "
              f"{kb:>7.1f} {x['bytes'] / 1e6:>8.1f} {x.get('missing', 0):>7} "
              f"{crc:>7}  {via}")
        w("  missing: indexed frames whose bytes are not in the .mjpeg (a cut-off")
        w("  tail, or a damaged index line). crc bad: frames whose bytes are damaged")
        w("  ('-' without --verify, which reads the whole video, or for a session")
        w("  recorded before frames carried a CRC).")

    if r["apriltag"]:
        w("\napriltag")
        w(f"  {'cam':<8} {'records':>7} {'rate':>6} {'gaps':>6} {'meta':>5} "
          f"{'tags/rec':>8}")
        for c, x in r["apriltag"].items():
            w(f"  {c:<8} {x['n']:>7} {_f(x['rate_hz']):>6} {x['gaps']:>6} "
              f"{x.get('meta', 0):>5} "
              f"{x.get('tags', 0) / max(x['n'], 1):>8.2f}")

    rec = r["recorder"]
    drops = rec["dropped"]
    w("\nrecorder")
    w(f"  queue drops {sum(drops.values()) if drops else 0}"
      f"{' ' + json.dumps(drops) if drops else ''}   "
      f"write errors {rec['write_errors'] if rec['write_errors'] is not None else '?'}   "
      f"wall-clock steps {len(rec['wall_clock_steps_ms'])}"
      f"{' ' + str(rec['wall_clock_steps_ms'][:5]) + ' ms' if rec['wall_clock_steps_ms'] else ''}")

    if r["sync"]:
        w("\nsync: capture time on the Jetson's CLOCK_MONOTONIC")
        w(f"  {'device':<28} {'clock':<14} {'segs':>4} {'drift ppm':>9} "
          f"{'delay p50':>9} {'p95':>7} {'max':>7}  ms")
        for d, x in r["sync"].items():
            lat = x["latency_ms"]
            w(f"  {d:<28} {x['anchor']:<14} {x['segments']:>4} "
              f"{_f(x['drift_ppm'], '{:+.1f}'):>9} {_f(lat.get('p50'), '{:.2f}'):>9} "
              f"{_f(lat.get('p95'), '{:.2f}'):>7} {_f(lat.get('max'), '{:.1f}'):>7}")
        w("  drift: + means the sender's clock runs fast; '-' under a minute of data.")
        w("  delay: above the fastest message in each window for a sender's own")
        w("  clock (so ~0 at best); capture-to-arrival for apriltag. Devices align")
        w("  to within the difference of their minimum one-way delays.")
    return "\n".join(out)


def render_compare(c: dict[str, Any], a: str, b: str) -> str:
    if not c["matched"]:
        return f"\ncompare: no messages matched between {a} and {b}"
    out = [f"\ncompare: {b} against {a}",
           f"  {c['matched']} messages matched; common shift {c['shift_s']:.6f} s",
           f"  alignment error |p50| {c['abs_p50_ms']:.3f} ms  |p95| "
           f"{c['abs_p95_ms']:.3f} ms  worst device bias {c['max_bias_ms']:.3f} ms",
           f"  {'device':<28} {'matched':>8} {'bias ms':>9} {'|p95| ms':>9}"]
    for d, x in sorted(c["devices"].items()):
        out.append(f"  {d:<28} {x['matched']:>8} {x['bias_ms']:>+9.3f} "
                   f"{x['abs_p95_ms']:>9.3f}")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Summarise a recorded session and check its timing.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("session", help="session directory")
    p.add_argument("--compare", metavar="SESSION",
                   help="a recording of the same data (e.g. of its replay) to "
                        "check alignment against")
    p.add_argument("--window-s", type=float, default=WINDOW_S,
                   help="clock-mapping window (sync.py)")
    p.add_argument("--json", action="store_true",
                   help="print the report as JSON instead of text")
    p.add_argument("--verify", action="store_true",
                   help="read every frame and check its CRC-32 (reads the "
                        "whole video)")
    args = p.parse_args(argv)

    with Session(args.session) as sess:
        rep, fits = summarize(sess, args.window_s, verify=args.verify)
    cmp = None
    if args.compare:
        with Session(args.compare) as other:
            cmp = compare(fit_session(other, args.window_s), fits)
            rep["compare"] = {"against": other.name, **cmp}
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(render(rep))
        if cmp is not None:
            print(render_compare(cmp, rep["compare"]["against"], rep["session"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
