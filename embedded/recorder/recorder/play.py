#!/usr/bin/env python3
"""
Play a recorded session back into the real system, over the same transports
it arrived on, so the funnel and april-detect run unchanged with no boat:

    MQTT     every recorded message, republished to a broker
             (the funnel's mosquitto, or amqtt on a laptop)
    video    each camera's frames on its own ZMQ PUB, in the camera's own
             format (SENDER.md), for april-detect to connect to:
             cam1 -> tcp://*:5581, cam2 -> 5582, ...
    apriltag (--apriltag) the recorded detections on one PUB, for the funnel
             to connect to *instead of* running april-detect

    python -m recorder.play data/20261107T143000Z
    python -m recorder.play data/<s> --apriltag --no-video        # no detectors
    python -m recorder.play data/<s> --loop --start 30 --end 90

Then point the consumers at it:
    april-detect   APRIL_SOURCE=tcp://127.0.0.1:5581 (CAMn_SOURCE in compose)
    funnel         unchanged (MQTT); FUNNEL_APRILTAG_* as for live detectors

Timing -- what keeps the streams aligned:

  * One thread sends everything, merged in recorded arrival (`tm`) order,
    each message at an absolute deadline `play_t0 + (tm - rec_t0) / speed`
    on perf_counter. Deadlines are absolute, so lateness never accumulates
    (the funnel's compute loop makes the same choice). A message is prepared
    -- read, rewritten -- *before* its deadline, and lateness is reported.

  * Messages go out on their recorded *arrival* schedule, real jitter and
    all. The funnel and april-detect then work out capture times from the
    senders' clocks with their own estimators, exactly as they do live.

  * Sender clocks move with the timeline. Each device's `t_ms` (and a
    camera's `t_send_ms`) is shifted so its first value lands on the replay's
    own CLOCK_MONOTONIC at that device's first message, then advances with
    the recording, scaled by --speed. Every device keeps the offset to
    arrival time it had when recorded, so the estimators reach the same
    alignment; and the clocks only ever move forward -- across --loop passes
    and across repeated runs -- so the funnel's ClockEstimator, which has no
    reboot handling, never sees one go backwards.

  * Absolute wall-clock fields (`t` in a payload or header; `t`, `ts.*` and
    `started` in detector records) are shifted by the same amount, so every
    stream agrees on what "now" is.

  * On --loop, `seq` and `tick` carry on from where the last pass ended, so a
    pass boundary looks like nothing rather than a sender restart.

--verbatim sends every byte exactly as recorded, once. Restart the funnel
between verbatim runs: its clock estimator would hold the previous run's
offsets.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import threading
import time
from array import array
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import parse_cameras
from .format import (
    U32,
    CorruptFrameError,
    Event,
    Session,
    frame_header,
    json_obj,
    num,
)

LOOP_GAP_S = 1.0
"""Pause between --loop passes, as a sensor that went quiet briefly."""

DEFAULT_APRILTAG = "tcp://*:5561"


def default_video_binds(cams: list[str], base: int = 5580) -> dict[str, str]:
    """cam1 -> tcp://*:5581 ... by the number in the name where there is one,
    so cam3 is on 5583 even when it is the only camera recorded."""
    out: dict[str, str] = {}
    used: set[int] = set()
    rest = []
    for cam in cams:
        m = re.search(r"(\d+)$", cam)
        n = int(m.group(1)) if m else 0
        if 1 <= n <= 99 and base + n not in used:
            out[cam] = f"tcp://*:{base + n}"
            used.add(base + n)
        else:
            rest.append(cam)
    port = base + 1
    for cam in rest:
        while port in used:
            port += 1
        out[cam] = f"tcp://*:{port}"
        used.add(port)
    return out


# ---- the plan: one pass over the session before anything is sent ------- #

@dataclass
class Dev:
    first_tm: int
    first_ms: Optional[float] = None
    """First value of the device's own clock (`t_ms` / `t_send_ms`)."""
    seq_min: Optional[int] = None
    seq_max: Optional[int] = None

    def seq(self, s: Optional[int]) -> None:
        if s is None:
            return
        self.seq_min = s if self.seq_min is None else min(self.seq_min, s)
        self.seq_max = s if self.seq_max is None else max(self.seq_max, s)


@dataclass
class Plan:
    t0: int = 0
    """Recorded `tm` where playback starts."""
    t1: int = 0
    tw0: float = 0.0
    """Recorded wall time at t0."""
    n: dict[str, int] = field(default_factory=dict)
    devices: dict[str, Dev] = field(default_factory=dict)
    retained: dict[str, Event] = field(default_factory=dict)
    """Latest retained message per topic from *before* --start, so a replay
    that skips the start still begins with the seats' presence."""

    @property
    def span_ns(self) -> int:
        return max(self.t1 - self.t0, 0)

    @property
    def loop_ns(self) -> int:
        return self.span_ns + int(LOOP_GAP_S * 1e9)


def _seq(v: Any) -> Optional[int]:
    f = num(v)
    return None if f is None else int(f) % U32


def device_key(ev: Event) -> tuple[Optional[str], Optional[float], Optional[int]]:
    """(device, first-clock candidate in ms, seq) for one event."""
    if ev.kind == "mqtt":
        obj = json_obj(ev.payload)
        if obj is None:
            return None, None, None
        return "mqtt:" + ev.source, num(obj.get("t_ms")), _seq(obj.get("seq"))
    if ev.kind == "frame":
        h = frame_header(ev.pre)
        a = num(h.get("t_send_ms"))
        return "cam:" + ev.source, a if a is not None else num(h.get("t_ms")), \
            _seq(h.get("seq"))
    if ev.source == "apriltag":
        rec = json_obj(ev.payload) or {}
        return f"april:{rec.get('cam')}", None, _seq(rec.get("tick"))
    return None, None, None


def events(sess: Session, args: argparse.Namespace) -> Any:
    return sess.events(mqtt=not args.no_mqtt, apriltag=args.apriltag is not None,
                       video=not args.no_video, cameras=args.cameras)


def prescan(sess: Session, args: argparse.Namespace) -> Plan:
    plan = Plan()
    first: Optional[int] = None
    start_ns = int(args.start * 1e9)
    end_ns = None if args.end is None else int(args.end * 1e9)
    for ev in events(sess, args):
        if first is None:
            first = ev.tm
        rel = ev.tm - first
        if rel < start_ns:
            if ev.kind == "mqtt" and ev.rec.get("retain"):
                plan.retained[ev.source] = ev
            continue
        if end_ns is not None and rel > end_ns:
            break
        if not plan.n:
            plan.t0 = ev.tm
            plan.tw0 = float(ev.rec.get("tw") or time.time())
        plan.t1 = ev.tm
        plan.n[ev.kind] = plan.n.get(ev.kind, 0) + 1
        key, clk, seq = device_key(ev)
        if key is None:
            continue
        d = plan.devices.get(key)
        if d is None:
            d = plan.devices[key] = Dev(ev.tm)
        if d.first_ms is None and clk is not None:
            d.first_ms, d.first_tm = clk, ev.tm
        d.seq(seq)
    return plan


# ---- rewriting -------------------------------------------------------- #

class Timebase:
    """Recorded time -> replay time, for the schedule and for every clock
    inside the payloads."""

    def __init__(self, plan: Plan, speed: float, play_t0_ns: int,
                 play_wall0: float) -> None:
        self.plan = plan
        self.speed = speed
        self.play_t0_ns = play_t0_ns
        self.play_wall0 = play_wall0

    def at(self, tm: int, p: int = 0) -> int:
        """When to send a message recorded at `tm`, on pass `p`."""
        return self.play_t0_ns + int(((tm - self.plan.t0) + p * self.plan.loop_ns)
                                     / self.speed)

    def sender_ms(self, key: str, v_ms: float, p: int = 0) -> float:
        """A device's own clock, moved so its first value falls on this
        machine's monotonic clock at that device's first message."""
        d = self.plan.devices[key]
        assert d.first_ms is not None
        return (self.at(d.first_tm) / 1e6
                + (v_ms - d.first_ms + p * self.plan.loop_ns / 1e6) / self.speed)

    def epoch(self, t: float, p: int = 0) -> float:
        return self.play_wall0 + (t - self.plan.tw0
                                  + p * self.plan.loop_ns / 1e9) / self.speed

    def seq(self, key: str, s: int, p: int) -> int:
        d = self.plan.devices.get(key)
        if p == 0 or d is None or d.seq_min is None or d.seq_max is None:
            return s
        return (s + p * (d.seq_max - d.seq_min + 1)) % U32

    def _clocked(self, key: str) -> bool:
        d = self.plan.devices.get(key)
        return d is not None and d.first_ms is not None


def rewrite_mqtt(topic: str, payload: bytes, tb: Timebase, p: int) -> bytes:
    obj = json_obj(payload)
    if obj is None:
        return payload
    key = "mqtt:" + topic
    t_ms = num(obj.get("t_ms"))
    if t_ms is not None and tb._clocked(key):
        obj["t_ms"] = int(round(tb.sender_ms(key, t_ms, p))) % U32
    t = num(obj.get("t"))
    if t is not None and t > 0:
        obj["t"] = round(tb.epoch(t, p), 6)
    s = _seq(obj.get("seq"))
    if s is not None and p:
        obj["seq"] = tb.seq(key, s, p)
    return json.dumps(obj, separators=(",", ":")).encode()


def rewrite_header(cam: str, pre: list[bytes], tb: Timebase, p: int) -> list[bytes]:
    if not pre or pre[0][:1] != b"{":
        return pre
    md = json_obj(pre[0])
    if md is None:
        return pre
    wrapped = "msg" in md
    hdr = md.get("msg") if wrapped else md
    if not isinstance(hdr, dict):
        return pre                          # a legacy name-only sender
    key = "cam:" + cam
    if tb._clocked(key):
        for f in ("t_ms", "t_send_ms"):
            v = num(hdr.get(f))
            if v is not None:
                hdr[f] = round(tb.sender_ms(key, v, p), 3)
    t = num(hdr.get("t"))
    if t is not None and t > 0:
        hdr["t"] = round(tb.epoch(t, p), 6)
    s = _seq(hdr.get("seq"))
    if s is not None and p:
        hdr["seq"] = tb.seq(key, s, p)
    # json.dumps' default separators, as pyzmq's send_json (and imageZMQ).
    return [json.dumps(md).encode(), *pre[1:]]


def rewrite_april(topic: str, payload: bytes, tb: Timebase, p: int) -> bytes:
    rec = json_obj(payload)
    if rec is None:
        return payload
    for k in ("t", "started"):
        v = num(rec.get(k))
        if v is not None and v > 0:
            rec[k] = round(tb.epoch(v, p), 6)
    ts = rec.get("ts")
    if isinstance(ts, dict):
        for k, v in ts.items():
            f = num(v)
            if f is not None and f > 0:
                ts[k] = round(tb.epoch(f, p), 6)
    if topic == "apriltag" and p:
        s = _seq(rec.get("tick"))
        if s is not None:
            rec["tick"] = tb.seq(f"april:{rec.get('cam')}", s, p)
    return json.dumps(rec, separators=(",", ":")).encode()


# ---- output ------------------------------------------------------------- #

class Outputs:
    def __init__(self, args: argparse.Namespace, cams: list[str]) -> None:
        self.args = args
        self.cams = cams
        self.mqtt: Any = None
        self.video: dict[str, Any] = {}
        self.april: Any = None
        self.dropped: dict[str, int] = {}
        self._zmq: Any = None

    def open(self) -> None:
        a = self.args
        if not a.no_mqtt:
            import paho.mqtt.client as mqtt

            up = threading.Event()
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                            client_id=f"recorder-play-{id(self) & 0xffff:x}")
            c.on_connect = lambda *_: up.set()
            c.max_queued_messages_set(0)
            try:
                c.connect(a.mqtt_host, a.mqtt_port, keepalive=15)
            except OSError as e:
                raise SystemExit(f"[play] cannot reach the MQTT broker at "
                                 f"{a.mqtt_host}:{a.mqtt_port}: {e} "
                                 f"(--no-mqtt to skip MQTT)") from None
            c.loop_start()
            if not up.wait(5.0):
                raise SystemExit("[play] MQTT broker did not accept the "
                                 "connection within 5 s")
            self.mqtt = c
            log(f"[play] MQTT -> {a.mqtt_host}:{a.mqtt_port}")
        if self.cams or a.apriltag is not None:
            import zmq

            self._zmq = zmq
            ctx = zmq.Context.instance()
            binds = default_video_binds(self.cams)
            binds.update(a.video_bind)
            for cam in self.cams:
                s = ctx.socket(zmq.PUB)
                s.setsockopt(zmq.SNDHWM, a.sndhwm)
                s.setsockopt(zmq.LINGER, 0)
                s.bind(binds[cam])
                self.video[cam] = s
                log(f"[play] {cam} video -> PUB {binds[cam]}")
            if a.apriltag is not None:
                s = ctx.socket(zmq.PUB)
                s.setsockopt(zmq.SNDHWM, 64)
                s.setsockopt(zmq.LINGER, 0)
                s.bind(a.apriltag)
                self.april = s
                log(f"[play] apriltag records -> PUB {a.apriltag}")

    def _drop(self, k: str) -> None:
        self.dropped[k] = self.dropped.get(k, 0) + 1

    def mqtt_send(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        info = self.mqtt.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != 0:
            self._drop("mqtt")

    def zmq_send(self, sock: Any, parts: list[bytes], k: str) -> None:
        try:
            sock.send_multipart(parts, flags=self._zmq.NOBLOCK, copy=False)
        except self._zmq.Again:
            self._drop(k)       # at the HWM (a PUB with no subscriber
            #                     discards silently instead)

    def close(self) -> None:
        if self.mqtt is not None:
            time.sleep(0.3)                 # let QoS 1 publishes go out
            self.mqtt.disconnect()
            self.mqtt.loop_stop()
        for s in [*self.video.values(), self.april]:
            if s is not None:
                s.close(0)


# ---- the loop ----------------------------------------------------------- #

def log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


STOP = threading.Event()


def wait_until(target_ns: int) -> None:
    while not STOP.is_set():
        d = target_ns - time.perf_counter_ns()
        if d <= 0:
            return
        time.sleep(min(d / 1e9, 0.1))


def _pct(a: array, p: float) -> float:
    if not a:
        return 0.0
    s = sorted(a)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))] / 1e6


def play(sess: Session, args: argparse.Namespace) -> dict[str, Any]:
    plan = prescan(sess, args)
    if not plan.n:
        raise SystemExit("[play] nothing to play in that window")
    cams = [] if args.no_video else (args.cameras or sess.cameras())
    log(f"[play] {sess.name}: {plan.span_ns / 1e9:.1f} s, "
        + ", ".join(f"{v} {k}" for k, v in sorted(plan.n.items()))
        + f", at {args.speed:g}x" + (" (loop)" if args.loop else "")
        + (" VERBATIM" if args.verbatim else ""))

    out = Outputs(args, cams)
    out.open()
    log(f"[play] waiting {args.lead_in_s:g} s for subscribers to connect")
    time.sleep(args.lead_in_s)

    tb = Timebase(plan, args.speed, time.perf_counter_ns(), time.time())
    late = array("q")
    sent: dict[str, int] = {}
    corrupt: dict[str, int] = {}
    start_ns = int(args.start * 1e9)
    end_ns = None if args.end is None else int(args.end * 1e9)
    next_stats = time.monotonic() + args.stats_interval

    def prepare(ev: Event, p: int) -> Optional[tuple]:
        v = args.verbatim
        if ev.kind == "mqtt":
            pl = ev.payload if v else rewrite_mqtt(ev.source, ev.payload, tb, p)
            return ("mqtt", ev.source, pl, int(ev.rec.get("qos") or 0),
                    bool(ev.rec.get("retain")))
        if ev.kind == "frame":
            try:
                jpeg = sess.jpeg(ev)
            except CorruptFrameError as e:
                # Damaged on disk: skip it, as a frame lost in transit would
                # be -- its seq gap shows downstream -- rather than send garbage.
                corrupt[ev.source] = corrupt.get(ev.source, 0) + 1
                if corrupt[ev.source] <= 3:
                    log(f"[play] skipping a damaged frame: {e}")
                return None
            pre = ev.pre if v else rewrite_header(ev.source, ev.pre, tb, p)
            return ("frame", ev.source, [*pre, jpeg])
        pl = ev.payload if v else rewrite_april(ev.source, ev.payload, tb, p)
        return ("apriltag", ev.source, [ev.source.encode(), pl])

    def send(item: tuple) -> None:
        kind = item[0]
        if kind == "mqtt":
            out.mqtt_send(item[1], item[2], item[3], item[4])
        elif kind == "frame":
            out.zmq_send(out.video[item[1]], item[2], item[1])
        else:
            out.zmq_send(out.april, item[2], "apriltag")
        sent[kind] = sent.get(kind, 0) + 1

    try:
        for ev in plan.retained.values():
            item = prepare(ev, 0)
            if item is not None:
                send(item)
        p = 0
        while not STOP.is_set():
            first: Optional[int] = None
            for ev in events(sess, args):
                if STOP.is_set():
                    break
                if first is None:
                    first = ev.tm
                rel = ev.tm - first
                if rel < start_ns:
                    continue
                if end_ns is not None and rel > end_ns:
                    break
                item = prepare(ev, p)       # before the deadline, not after
                if item is None:
                    continue
                target = tb.at(ev.tm, p)
                wait_until(target)
                send(item)
                late.append(time.perf_counter_ns() - target)
                if args.stats_interval > 0 and time.monotonic() >= next_stats:
                    next_stats = time.monotonic() + args.stats_interval
                    log(f"[play] pass {p + 1}  {(ev.tm - plan.t0) / 1e9:6.1f}/"
                        f"{plan.span_ns / 1e9:.1f} s  "
                        + "  ".join(f"{k} {v}" for k, v in sorted(sent.items()))
                        + f"  late p95 {_pct(late, 95):.2f} ms")
            p += 1
            if not args.loop:
                break
            wait_until(tb.at(plan.t0, p))
    except KeyboardInterrupt:
        STOP.set()
    finally:
        out.close()

    summary = {
        "session": sess.name,
        "passes": p,
        "sent": sent,
        "dropped": out.dropped,
        "corrupt": corrupt,
        "late_ms": {"p50": round(_pct(late, 50), 3), "p95": round(_pct(late, 95), 3),
                    "max": round(max(late) / 1e6, 3) if late else 0.0},
    }
    log(f"[play] done: {json.dumps(summary)}")
    return summary


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Play a recorded session back over MQTT and ZMQ.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("session", help="session directory")
    p.add_argument("--mqtt-host", default="127.0.0.1")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--no-mqtt", action="store_true", help="do not replay MQTT")
    p.add_argument("--video-bind", default="",
                   help="cam=endpoint,... overriding the default "
                        "cam1=tcp://*:5581, cam2=tcp://*:5582, ...")
    p.add_argument("--cameras", default="",
                   help="comma-separated subset of cameras to play")
    p.add_argument("--no-video", action="store_true", help="do not replay video")
    p.add_argument("--apriltag", nargs="?", const=DEFAULT_APRILTAG, default=None,
                   metavar="BIND",
                   help="also publish the recorded detections, on BIND "
                        f"(default {DEFAULT_APRILTAG}); do not run "
                        "april-detect at the same time, it binds that port")
    p.add_argument("--speed", type=float, default=1.0,
                   help="playback rate; anything but 1 also compresses the "
                        "sensor clocks, so use 1 for timing work")
    p.add_argument("--loop", action="store_true", help="repeat until Ctrl-C")
    p.add_argument("--start", type=float, default=0.0,
                   help="seconds into the session to start at")
    p.add_argument("--end", type=float, default=None,
                   help="seconds into the session to stop at")
    p.add_argument("--lead-in-s", type=float, default=1.0,
                   help="wait after binding, so subscribers are connected "
                        "before the first message (ZMQ drops until they are)")
    p.add_argument("--verbatim", action="store_true",
                   help="send every byte exactly as recorded, once")
    p.add_argument("--sndhwm", type=int, default=8,
                   help="video PUB high-water mark (drops are counted)")
    p.add_argument("--stats-interval", type=float, default=5.0)
    a = p.parse_args(argv)
    if a.speed <= 0:
        p.error("--speed must be positive")
    if a.verbatim and a.loop:
        p.error("--verbatim replays once: looping would run every sender's "
                "clock backwards at each pass")
    a.video_bind = parse_cameras(a.video_bind)
    a.cameras = [c.strip() for c in a.cameras.split(",") if c.strip()] or None
    return a


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    if args.verbatim and args.speed != 1.0:
        log("[play] warning: --verbatim at a speed other than 1 leaves the "
            "sender clocks running at 1x against a faster schedule")
    with Session(args.session) as sess:
        play(sess, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
