#!/usr/bin/env python3
"""
Telling the cameras' own recorders which session is running.

A vision/cam-sender run with CAM_RECORD=true also writes every frame to its
own SD card (cam_sender/localrec.py). A Wi-Fi outage then costs the Jetson's
copy of that camera, not the camera's. This module publishes the state those
local recorders follow, on one ZMQ PUB the cameras connect to
(REC_CAM_CONTROL_BIND, :5580; their CAM_REC_CONTROL):

    {"v": 1, "type": "rec", "sync": true, "session": "20261107T143000Z_x" | null,
     "state": 7, "tm": <perf_counter_ns>, "tw": <time.time()>}

It is sent on every change and every REC_CAM_CONTROL_INTERVAL_S.

State, not events. A camera that was unreachable at start or stop, or has
restarted since, is put right by the next message. A camera that hears
nothing keeps doing what it was doing, so an outage never stops a camera's
recording. Only `session: null`, or a different session, ends one.

REC_CAM_SYNC=false keeps the socket but always says null, so every camera
stays or goes idle and its local recorder is effectively off, with nothing
to change on the cameras. An empty REC_CAM_CONTROL_BIND opens no socket.

`tm`/`tw` are this host's clocks at the send. The cameras keep them beside
their own receipt stamps, as a coarse clock anchor.

Confirmation travels the other way, inside the frames. A frame a camera is
also keeping carries `"rec": <session>` in its header, which VideoTap reads
(taps.py) and /recorder/status shows.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

from . import log


class CamSync:
    def __init__(self, bind: str, enabled: bool = True,
                 interval_s: float = 1.0) -> None:
        self.bind = bind.strip()
        self.enabled = enabled
        self.interval_s = max(0.05, interval_s)
        self.state = 0
        """Bumped on every change of session."""
        self.changed_tw: Optional[float] = None
        self.n_sent = 0
        self.last_error: Optional[str] = None
        self._session: Optional[str] = None
        self._cv = threading.Condition()
        self._halt = False
        self._sock: Any = None
        self._thread: Optional[threading.Thread] = None

    @property
    def session(self) -> Optional[str]:
        """What the cameras are told: the session, unless sync is off."""
        return self._session if self.enabled else None

    def start(self) -> None:
        if not self.bind:
            log.info("[camsync] REC_CAM_CONTROL_BIND is empty: cameras are not told "
                     "about sessions")
            return
        import zmq

        sock = zmq.Context.instance().socket(zmq.PUB)
        sock.setsockopt(zmq.LINGER, 500)          # so the last idle gets out
        sock.setsockopt(zmq.SNDHWM, 16)
        try:
            sock.bind(self.bind)
        except zmq.ZMQError as e:
            # Recording on the Jetson matters more than telling the cameras.
            sock.close(0)
            self.last_error = f"bind {self.bind}: {e}"
            log.error(f"[camsync] cannot bind {self.bind}: {e}; cameras will not "
                      "record locally")
            return
        self._sock = sock
        self._thread = threading.Thread(target=self._run, name="camsync",
                                        daemon=True)
        self._thread.start()
        log.info(f"[camsync] PUB bind {self.bind}: camera sync "
                 + ("on" if self.enabled else "OFF (REC_CAM_SYNC=false): cameras "
                    "are told to stay idle"))

    def set(self, session: Optional[str]) -> None:
        """The session now recording, or None. Never blocks: the thread
        sends it."""
        with self._cv:
            if session == self._session:
                return
            self._session = session
            self.state += 1
            self.changed_tw = time.time()
            self._cv.notify()

    def message(self) -> bytes:
        return json.dumps({
            "v": 1, "type": "rec", "sync": self.enabled, "session": self.session,
            "state": self.state, "tm": time.perf_counter_ns(),
            "tw": round(time.time(), 6),
        }, separators=(",", ":")).encode("utf-8")

    def _send(self) -> None:
        import zmq

        try:
            self._sock.send(self.message(), zmq.NOBLOCK)
            self.n_sent += 1
        except zmq.ZMQError as e:
            self.last_error = repr(e)

    def _run(self) -> None:
        sent = -1
        while True:
            with self._cv:
                if self._halt:
                    return
                if self.state == sent:
                    self._cv.wait(self.interval_s)        # set() wakes it early
                    if self._halt:
                        return
                sent = self.state
            self._send()

    def close(self) -> None:
        """Tell the cameras nothing is recording, then close the socket."""
        with self._cv:
            if self._session is not None:
                self._session = None
                self.state += 1
            self._halt = True
            self._cv.notify()
        if self._thread is not None:
            self._thread.join(2.0)
        if self._sock is not None:
            self._send()
            self._sock.close()                    # lingers up to 500 ms
            self._sock = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "bind": self.bind or None,
            "listening": self._sock is not None,
            "session": self.session,
            "state": self.state,
            "sent": self.n_sent,
            "last_error": self.last_error,
        }
