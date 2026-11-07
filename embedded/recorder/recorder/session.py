#!/usr/bin/env python3
"""
Sessions: start, stop, and what is going on right now.

The taps run all the time and hand every item to `offer()`. With no session
active it is discarded; with one, it is queued for that session's writer.
`offer()` reads one attribute and takes no lock, so the receivers never wait
on a start or stop in progress.

A session is one directory named after its UTC start time (format.py). Its
meta.json is written at start with `complete: false` and rewritten at stop
with the clock anchors, the stop reason, and a per-device summary of what was
received, written, lost in transit (sequence gaps) and dropped here (queue).
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from . import __version__, log
from .config import Settings
from .format import (
    FORMAT_VERSION,
    Item,
    SessionWriter,
    create_session_dir,
    read_meta,
    stamp,
    write_meta,
)
from .taps import MqttTap, Tap, VideoTap, diff
from .writer import WriteQueue, Writer, disk_free, storage_info


class BusyError(Exception):
    """A session is already recording."""


class IdleError(Exception):
    """No session is recording."""


class LowDiskError(Exception):
    """Not enough free space to start."""


@dataclass
class Active:
    name: str
    path: Path
    meta: dict[str, Any]
    files: SessionWriter
    queue: WriteQueue
    writer: Writer
    baseline: dict[str, dict[str, tuple[int, int, int, int]]]
    tm0: int
    t_start: float


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


class SessionManager:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.root = Path(s.dir)
        self._lock = threading.Lock()
        self._active: Optional[Active] = None
        self.mqtt: Optional[MqttTap] = None
        self.taps: list[Tap] = []
        self.last: Optional[dict[str, Any]] = None
        """Summary of the most recently stopped session."""
        self.storage = storage_info(self.root)
        """The volume REC_DIR is on, and why it is a bad one if it is."""

    def attach(self, mqtt: Optional[MqttTap], taps: list[Tap]) -> None:
        self.mqtt = mqtt
        self.taps = taps

    @property
    def recording(self) -> bool:
        return self._active is not None

    # -- the receivers' side -------------------------------------------------- #
    def offer(self, item: Item) -> None:
        a = self._active
        if a is not None:
            a.queue.put(item)

    # -- control -------------------------------------------------------------- #
    def start(self, label: str = "") -> dict[str, Any]:
        with self._lock:
            if self._active is not None:
                raise BusyError(self._active.name)
            self.root.mkdir(parents=True, exist_ok=True)
            free = disk_free(self.root)
            if free < self.s.min_free_gb * 1e9:
                raise LowDiskError(f"{free / 1e9:.2f} GB free in {self.root}, "
                              f"REC_MIN_FREE_GB is {self.s.min_free_gb:g}")
            path = create_session_dir(self.root, label)
            self.storage = storage_info(path)
            tm0, tw0 = stamp()
            meta = {
                "format": FORMAT_VERSION,
                "recorder": __version__,
                "name": path.name,
                "label": label or None,
                "complete": False,
                "started": _iso(tw0),
                "start": {"tm": tm0, "tw": round(tw0, 6)},
                "host": socket.gethostname(),
                "storage": self.storage,
                "clock": {
                    "tm": "time.perf_counter_ns(): " + time.get_clock_info(
                        "perf_counter").implementation,
                    "tw": "time.time(), unix seconds",
                },
                "sources": {
                    "mqtt": {"broker": f"{self.s.mqtt_host}:{self.s.mqtt_port}",
                             "topics": self.s.topics} if self.mqtt else None,
                    "cameras": self.s.camera_map,
                    "apriltag": self.s.apriltag_endpoints,
                },
                "settings": self.s.model_dump(),
            }
            write_meta(path, meta)
            files = SessionWriter(path)
            q = WriteQueue(int(self.s.queue_mb * 1e6))
            writer = Writer(files, q, flush_s=self.s.flush_s,
                            fsync_s=self.s.fsync_s,
                            disk_check_s=self.s.disk_check_s,
                            min_free_bytes=int(self.s.min_free_gb * 1e9),
                            on_low_disk=self._low_disk)
            writer.start()
            baseline = {t.key: t.counters.totals() for t in self.taps}
            self._active = Active(path.name, path, meta, files, q, writer,
                                  baseline, tm0, time.monotonic())
        # Outside the lock: the retained messages it brings back arrive on
        # paho's thread and go straight into the session just opened.
        if self.mqtt is not None:
            self.mqtt.resubscribe()
        log.info(f"[rec] recording to {path}")
        if self.storage["warning"]:
            log.warn(f"[storage] {self.storage['warning']}")
        return {"session": path.name, "path": str(path),
                "started": meta["started"]}

    def stop(self, reason: str = "request") -> dict[str, Any]:
        with self._lock:
            a = self._active
            if a is None:
                raise IdleError()
            self._active = None             # receivers stop offering here
            a.writer.finish()
            a.files.close()
            tm1, tw1 = stamp()
            summary = self._summary(a, tm1)
            a.meta.update({
                "complete": True,
                "stopped": _iso(tw1),
                "end": {"tm": tm1, "tw": round(tw1, 6)},
                "duration_s": round((tm1 - a.tm0) / 1e9, 3),
                "stop_reason": reason,
                "summary": summary,
            })
            write_meta(a.path, a.meta)
            self.last = {"session": a.name, "path": str(a.path),
                         "duration_s": a.meta["duration_s"],
                         "stop_reason": reason, **summary}
        log.info(f"[rec] stopped {a.name} ({reason}): "
                 f"{a.meta['duration_s']:.1f}s, "
                 f"{summary['written_mb']:.1f} MB, "
                 f"{sum(summary['dropped'].values())} dropped")
        return self.last

    def _low_disk(self, _free: int) -> None:
        # Called on the writer thread, which stop() joins: hand it off.
        def go() -> None:
            try:
                self.stop("low disk")
            except IdleError:
                pass

        threading.Thread(target=go, name="stop-low-disk", daemon=True).start()

    def _summary(self, a: Active, tm1: int) -> dict[str, Any]:
        received: dict[str, Any] = {}
        for t in self.taps:
            base = a.baseline.get(t.key, {})
            for dev, d in diff(t.counters.totals(), base).items():
                received[dev] = d
        return {
            "received": received,
            "written": dict(a.files.written),
            "written_mb": round(a.files.nbytes / 1e6, 2),
            "dropped": dict(a.queue.dropped),
            "write_errors": a.writer.n_errors,
            "last_write_error": a.writer.last_error,
        }

    # -- reading -------------------------------------------------------------- #
    def status(self) -> dict[str, Any]:
        a = self._active
        try:
            free = disk_free(self.root if self.root.exists() else self.root.parent)
        except OSError:
            free = None
        sources: dict[str, Any] = {"video": {}}
        for t in self.taps:
            if isinstance(t, VideoTap):
                sources["video"][t.cam] = t.status()
            else:
                sources[t.name] = t.status()
        out: dict[str, Any] = {
            "recording": a is not None,
            "session": None,
            "disk_free_gb": None if free is None else round(free / 1e9, 2),
            "storage": self.storage,
            "sources": sources,
            "last": self.last,
        }
        if a is not None:
            out["session"] = {
                "name": a.name,
                "path": str(a.path),
                "started": a.meta["started"],
                "elapsed_s": round(time.monotonic() - a.t_start, 1),
                "written_mb": round(a.files.nbytes / 1e6, 2),
                "written": dict(a.files.written),
                "queued": len(a.queue),
                "queued_mb": round(a.queue.nbytes / 1e6, 2),
                "dropped": dict(a.queue.dropped),
                "write_errors": a.writer.n_errors,
            }
        return out

    def sessions(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.iterdir(), reverse=True):
            if not p.is_dir():
                continue
            m = read_meta(p)
            if not m:
                continue
            size = sum(f.stat().st_size for f in p.iterdir() if f.is_file())
            out.append({
                "name": p.name,
                "label": m.get("label"),
                "started": m.get("started"),
                "duration_s": m.get("duration_s"),
                "complete": bool(m.get("complete")),
                "recording": self._active is not None and self._active.path == p,
                "stop_reason": m.get("stop_reason"),
                "size_mb": round(size / 1e6, 2),
            })
        return out
