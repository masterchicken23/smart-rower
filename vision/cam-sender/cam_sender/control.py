#!/usr/bin/env python3
"""
Hearing the recorder: which session, if any, the Jetson is recording.

embedded/recorder (recorder/camsync.py) publishes its state as one JSON
message, on every change and once a second:

    {"v": 1, "type": "rec", "sync": true, "session": "20261107T143000Z_x" | null,
     "state": 7, "tm": <its perf_counter_ns>, "tw": <its time.time()>}

State, not events: whatever this camera missed while it was disconnected or
restarting, the next message puts right. Silence changes nothing -- a camera
that stops hearing the recorder keeps recording, because a Wi-Fi outage is
exactly what the local copy is for. Only `session: null` (a stop, or the
recorder running with REC_CAM_SYNC=false) or a different session ends one.

Entirely off the frame loop. The socket lives on this thread and in its own
ZMQ context: libzmq resolves a host name (`jetson.local`) on the context's
I/O thread, and an mDNS lookup with the network down can block for seconds.
In the shared context that thread is also the one writing frames.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from .log import log

WARN_SILENT_S = 10.0


def parse(raw: bytes) -> Optional[dict[str, Any]]:
    """A control message, or None if this is not one."""
    try:
        m = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(m, dict) or m.get("type") != "rec" or m.get("v") != 1:
        return None
    s = m.get("session")
    if s is not None and not isinstance(s, str):
        return None
    return m


class ControlListener:
    """SUB on the recorder's control address, on a thread of its own. Hands
    every state it hears to `on_state(session_or_None, anchor)`, where
    `anchor` pairs the recorder's clock stamps with this camera's at receipt.

    It owns a Thread rather than being one: a subclass shares its namespace
    with Thread's private attributes, which change between Python versions
    (3.13's Thread.__init__ sets `self._handle`, which silently replaced a
    method of that name here)."""

    def __init__(self, endpoint: str,
                 on_state: Callable[[Optional[str], dict], None],
                 halt: threading.Event, *,
                 now_ns: Callable[[], int] = time.perf_counter_ns,
                 warn_s: float = WARN_SILENT_S) -> None:
        self.endpoint = endpoint
        self.on_state = on_state
        self._halt = halt
        self._thread = threading.Thread(target=self._run, name="rec-control",
                                        daemon=True)
        self._now_ns = now_ns
        self.warn_s = warn_s
        self.n = 0
        self.n_bad = 0
        self.sync: Optional[bool] = None
        self.session: Optional[str] = None
        """The session the recorder last said it was recording."""
        self.last_heard: Optional[float] = None
        self._t0 = time.monotonic()
        self._warned = False

    def silent_s(self) -> float:
        """Seconds since the last message (since start, if none yet)."""
        ref = self.last_heard if self.last_heard is not None else self._t0
        return time.monotonic() - ref

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        import zmq

        ctx = zmq.Context(1)                # own I/O thread: see module doc
        sock = ctx.socket(zmq.SUB)
        try:
            sock.setsockopt(zmq.SUBSCRIBE, b"")
            sock.setsockopt(zmq.RCVTIMEO, 500)
            sock.setsockopt(zmq.LINGER, 0)
            # Back off while the Jetson is unreachable, rather than hammering
            # the resolver ten times a second.
            sock.setsockopt(zmq.RECONNECT_IVL, 1000)
            sock.setsockopt(zmq.RECONNECT_IVL_MAX, 10000)
            # ZMTP heartbeats, so a link that died silently (no FIN, as when
            # Wi-Fi drops) is noticed and reconnected. libzmq >= 4.2.
            for opt, val in (("HEARTBEAT_IVL", 2000), ("HEARTBEAT_TIMEOUT", 6000)):
                o = getattr(zmq, opt, None)
                if o is not None:
                    try:
                        sock.setsockopt(o, val)
                    except zmq.ZMQError:
                        pass
            try:
                sock.connect(self.endpoint)
            except zmq.ZMQError as e:
                log(f"[rec] control: cannot connect {self.endpoint}: {e}; "
                    "local recording will not start")
                return
            log(f"[rec] control: SUB connect {self.endpoint}")
            while not self._halt.is_set():
                try:
                    raw = sock.recv()
                except zmq.Again:
                    self._check_silence()
                    continue
                # Only the newest state matters: skip anything queued behind it.
                while True:
                    try:
                        raw = sock.recv(zmq.NOBLOCK)
                    except zmq.Again:
                        break
                self._on_message(raw)
        except Exception as e:              # noqa: BLE001
            if not self._halt.is_set():
                log(f"[rec] control: stopped: {e!r}")
        finally:
            sock.close(0)
            ctx.term()

    def _on_message(self, raw: bytes) -> None:
        tm, tw = self._now_ns(), time.time()
        msg = parse(raw)
        if msg is None:
            self.n_bad += 1
            if self.n_bad <= 3:
                log(f"[rec] control: ignoring a message that is not a "
                    f"recorder state ({raw[:60]!r})")
            return
        if self._warned:
            log(f"[rec] control: hearing the recorder again after "
                f"{self.silent_s():.0f} s")
            self._warned = False
        self.last_heard = time.monotonic()
        self.n += 1
        sync = msg.get("sync") is not False
        if sync != self.sync:
            if not sync:
                log("[rec] control: the recorder has camera sync off "
                    "(REC_CAM_SYNC=false); not recording locally")
            elif self.sync is not None:
                log("[rec] control: the recorder has camera sync on again")
            self.sync = sync
        session = msg.get("session") if sync else None
        self.session = session
        anchor = {
            "recorder": {"tm": msg.get("tm"), "tw": msg.get("tw"),
                         "state": msg.get("state")},
            "local": {"tm": tm, "tw": round(tw, 6)},
        }
        try:
            self.on_state(session, anchor)
        except Exception as e:              # noqa: BLE001
            log(f"[rec] control: {e!r}")

    def _check_silence(self) -> None:
        if self._warned or self.silent_s() < self.warn_s:
            return
        self._warned = True
        what = "nothing heard yet from" if self.last_heard is None else "no word from"
        log(f"[rec] control: {what} the recorder at {self.endpoint} for "
            f"{self.silent_s():.0f} s; keeping the current state")
