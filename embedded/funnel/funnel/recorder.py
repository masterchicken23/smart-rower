#!/usr/bin/env python3
"""
Raw sample recording, for debug runs only.

Off by default (FUNNEL_RECORD_ENABLED=false) and free when off: the Recorder is
never constructed, so ingest's only cost is one `is not None` test per sample.
Enabled, it writes a session directory:

    <FUNNEL_RECORD_DIR>/<utc-session>/
        meta.json              config and start time
        rower_3_seat.jsonl     one JSON object per sample
        boat_imu.jsonl

JSONL per stream rather than one interleaved file: streams arrive at unrelated
rates, and a per-stream file can be read back, replayed or plotted on its own.
This is the same role the captures in experiments/imu-recording/recordings/
play, in a format that does not assume a fixed column set.

Writes go through a bounded queue that drops the *oldest* entry when full, so a
slow SD card can never apply back-pressure to ingest -- the same policy as the
publisher queue in experiments/network-video-yolo/pose_stream.py. Recording is
a debug convenience; losing a sample from the log is always preferable to
delaying the live pipeline.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional, TextIO

from . import log
from .models import Sample, StreamKey


def session_name(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(UTC)
    return now.strftime("%Y%m%dT%H%M%SZ")


class Recorder:
    def __init__(self, directory: str, queue_max: int = 4096,
                 flush_s: float = 1.0) -> None:
        self.root = Path(directory)
        self.queue_max = queue_max
        self.flush_s = flush_s
        self.session: Optional[Path] = None
        self._q: deque = deque()
        self._files: dict[str, TextIO] = {}
        self._wake = asyncio.Event()
        self.n_written = 0
        self.n_dropped = 0

    # -- lifecycle ---------------------------------------------------------- #
    def start(self, meta: Optional[dict[str, Any]] = None) -> Path:
        self.session = self.root / session_name()
        self.session.mkdir(parents=True, exist_ok=True)
        payload = {
            "started": datetime.now(UTC).isoformat(),
            "started_monotonic": time.monotonic(),
            **(meta or {}),
        }
        (self.session / "meta.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8")
        log.info(f"[rec] recording to {self.session}")
        return self.session

    def close(self) -> None:
        self._drain()
        for f in self._files.values():
            try:
                f.close()
            except Exception:               # noqa: BLE001
                pass
        self._files.clear()
        log.info(f"[rec] closed: {self.n_written} samples written, "
                 f"{self.n_dropped} dropped")

    # -- ingest side (synchronous, must stay cheap) ------------------------- #
    def offer(self, key: StreamKey, sample: Sample) -> None:
        """Queue a sample. Never blocks, never raises."""
        if len(self._q) >= self.queue_max:
            self._q.popleft()               # drop oldest: stay current
            self.n_dropped += 1
        self._q.append((key, sample))
        if not self._wake.is_set():
            self._wake.set()

    # -- writer task -------------------------------------------------------- #
    async def run(self) -> None:
        if self.session is None:
            self.start()
        try:
            while True:
                try:
                    await asyncio.wait_for(self._wake.wait(), self.flush_s)
                except TimeoutError:
                    pass
                self._wake.clear()
                self._drain()
                for f in self._files.values():
                    f.flush()
        except asyncio.CancelledError:
            self.close()
            raise

    def _drain(self) -> None:
        while self._q:
            key, sample = self._q.popleft()
            try:
                self._write(key, sample)
            except Exception as e:          # noqa: BLE001
                # A full or read-only volume must not take the service down.
                self.n_dropped += 1
                if self.n_dropped % 500 == 1:
                    log.error(f"[rec] write failed: {e!r}")

    def _write(self, key: StreamKey, sample: Sample) -> None:
        f = self._file(key)
        rec = {
            "t_recv": round(sample.t_recv, 6),
            "t": None if sample.t_src is None else round(sample.t_src, 6),
            "seq": sample.seq,
            "values": sample.values,
        }
        if sample.dev:
            rec["dev"] = sample.dev
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.n_written += 1

    def _file(self, key: StreamKey) -> TextIO:
        name = key.as_path().replace("/", "_") + ".jsonl"
        f = self._files.get(name)
        if f is None:
            assert self.session is not None
            f = open(self.session / name, "a", encoding="utf-8")
            self._files[name] = f
        return f

    def stats(self) -> dict[str, Any]:
        return {
            "session": None if self.session is None else str(self.session),
            "written": self.n_written,
            "dropped": self.n_dropped,
            "queued": len(self._q),
        }
