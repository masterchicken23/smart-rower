#!/usr/bin/env python3
"""
Local recording: every frame this camera sends, also written to its own disk,
so a session survives the Wi-Fi link the Jetson's copy depends on.

Sessions are started and stopped by embedded/recorder (control.py hears it)
and named exactly as the recorder names them. They are written in the
recorder's own session format (embedded/recorder/recorder/format.py, v1), so
`python -m recorder.report` and `recorder.play` read a camera's session as
they read the Jetson's:

    <record-dir>/<session>/          the Jetson's session name, unchanged
        meta.json                    segments, stop reasons, counts, anchors
        <cam>.mjpeg                  the JPEGs as sent, concatenated
        <cam>.idx.jsonl              {"tm","tw","off","len","crc","pre"} per frame

`pre` is the frame's header exactly as it was sent (seq, t_ms, t_send_ms,
rec), so a frame here matches its copy on the Jetson by `seq`. `tm` is
`t_send_ms` in ns, on this Pi's clock rather than the Jetson's: keep a
camera's session in a directory of its own, never merge its files into the
Jetson's session (whose `tm` is the Jetson's clock).

A cam-sender restarted mid-session hears the same session again and resumes
it: the files are append-only and each frame's offset is read from the file,
so a resumed session is one session with a second entry in `segments`.

Threads. The frame loop calls Session.offer() and nothing else: a lock and a
deque append. The writer thread does the writes, CRCs, flushes and fsyncs.
Opening and closing a session (mkdir, meta.json, the final drain and fsync)
run on the caller of LocalRecorder.want() / shutdown() -- the control thread
or shutdown -- never on the frame loop. A slow or stalled SD card therefore
costs recorded frames (oldest dropped first, counted, as recorder/writer.py
does), never sent ones.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import threading
import time
import zlib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .log import log

FORMAT_VERSION = 1
"""embedded/recorder/recorder/format.py FORMAT_VERSION."""
META = "meta.json"
VIDEO_EXT = ".mjpeg"
INDEX_EXT = ".idx.jsonl"

SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
"""What a session directory may be called. Every name the recorder makes
(`20261107T143000Z_pause-drill`, format.session_name) fits; nothing with a
path separator does, so a control message cannot write outside --record-dir."""

MAX_WRITE_ERRORS = 50
"""Consecutive failed frame writes (~3 s at 15 fps) that end a session: the
card is failing, and retrying every frame only adds load."""

GUARDS = ("low disk", "max duration", "write errors")
"""Stop reasons that are this camera's own decision. A session stopped for
one of them is not restarted by the recorder's next message -- nor resumed by
a restarted cam-sender, which finds the reason in meta.json."""


def valid_name(name: Any) -> bool:
    return isinstance(name, str) and SESSION_NAME.match(name) is not None


def disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _line(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":")) + "\n"


def _enc(b: bytes) -> Any:
    """A header as the recorder's format stores it (format.enc_bytes)."""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(b).decode("ascii")}


def _write_all(f: Any, data: bytes) -> None:
    """Unbuffered write, looping over short writes."""
    mv = memoryview(data)
    while mv:
        n = f.write(mv)
        if not n:
            raise OSError("write made no progress")
        mv = mv[n:]


def read_meta(path: Path) -> dict:
    try:
        obj = json.loads((path / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def write_meta(path: Path, meta: dict) -> None:
    """Atomically, so a crash leaves the previous meta.json, never half of one."""
    tmp = path / (META + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path / META)


class _Frame:
    __slots__ = ("jpeg", "pre", "tm", "tw")

    def __init__(self, pre: bytes, jpeg: bytes, tm: int, tw: float) -> None:
        self.pre = pre
        self.jpeg = jpeg
        self.tm = tm
        self.tw = tw

    @property
    def size(self) -> int:
        return len(self.jpeg) + len(self.pre) + 128


class WriteQueue:
    """Frames waiting for the disk, bounded in bytes. When it is full the
    *oldest* waiting frame is dropped and counted (recorder/writer.py): a
    recording with a counted hole is better than a frame loop that waits."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(1, int(max_bytes))
        self._cv = threading.Condition()
        self._q: deque = deque()
        self.nbytes = 0
        self.dropped = 0
        self.late = 0
        """Frames offered after the session began to close: not written."""
        self.closed = False

    def put(self, item: _Frame) -> None:
        """Never blocks for longer than a lock acquire, never raises."""
        size = item.size
        with self._cv:
            if self.closed:
                self.late += 1
                return
            self._q.append((item, size))
            self.nbytes += size
            while self.nbytes > self.max_bytes and len(self._q) > 1:
                _, osize = self._q.popleft()
                self.nbytes -= osize
                self.dropped += 1
            self._cv.notify()

    def take_all(self, timeout: float) -> list:
        with self._cv:
            if not self._q:
                self._cv.wait(timeout)
            items = [it for it, _ in self._q]
            self._q.clear()
            self.nbytes = 0
            return items

    def close(self) -> None:
        with self._cv:
            self.closed = True
            self._cv.notify()

    def __len__(self) -> int:
        return len(self._q)


class Session:
    """One open session: its files, its queue and its writer thread.

    Construction opens it (mkdir, meta.json, the files) and starts the
    writer; close() drains, fsyncs and finalises meta.json. Neither runs on
    the frame loop, whose whole interface is offer()."""

    def __init__(self, root: Path, name: str, cam: str, *,
                 now_ns: Callable[[], int], queue_bytes: int, flush_s: float,
                 fsync_s: float, disk_check_s: float, min_free_bytes: int,
                 max_s: float, settings: dict, control: Optional[dict],
                 on_guard: Callable[[Session, str], None],
                 free: Callable[[Path], int] = disk_free) -> None:
        self.name = name
        self.cam = cam
        self.path = root / name
        self.queue = WriteQueue(queue_bytes)
        self.written = 0
        self.nbytes = 0
        self.write_errors = 0
        self.last_error: Optional[str] = None
        self._now_ns = now_ns
        self._flush_s = max(flush_s, 0.05)
        self._fsync_s = fsync_s
        self._disk_check_s = disk_check_s
        self._min_free = min_free_bytes
        self._max_s = max_s
        self._on_guard = on_guard
        self._free = free
        self._guarded = False
        self._consecutive = 0
        self._finish = threading.Event()

        self.path.mkdir(parents=True, exist_ok=True)
        old = read_meta(self.path)
        self.resumed = bool(old.get("segments"))
        tw0, tm0 = time.time(), now_ns()
        self.t_start = time.monotonic()
        self.segment: dict[str, Any] = {
            "started": _iso(tw0), "start": {"tm": tm0, "tw": round(tw0, 6)},
            "resumed": self.resumed, "control_start": control,
        }
        if self.resumed:
            self.meta = old
        else:
            self.meta = {
                "format": FORMAT_VERSION,
                "writer": "cam-sender",
                "name": name,
                "camera": cam,
                "host": socket.gethostname(),
                "started": self.segment["started"],
                "start": self.segment["start"],
                "clock": {
                    "tm": "the frame header's t_send_ms, in ns, on this "
                          "camera's clock (not the Jetson's)",
                    "tw": "time.time() on this camera, unix seconds",
                },
                "settings": settings,
                "segments": [],
            }
        self.meta["complete"] = False
        self.meta["stop_reason"] = None
        self.meta["segments"].append(self.segment)
        write_meta(self.path, self.meta)

        # Unbuffered and append-only: each frame goes to the OS in one write,
        # before its index line exists (recorder/format.py, SessionWriter).
        self._video = open(self.path / (cam + VIDEO_EXT), "ab", buffering=0)  # noqa: SIM115
        try:
            idx = self.path / (cam + INDEX_EXT)
            torn = self._ends_mid_line(idx)
            self._index = open(idx, "a", encoding="utf-8")  # noqa: SIM115
            if torn:
                # A crash cut the last line short; start ours on a fresh one
                # so only the torn line is lost, not the first of this segment.
                self._index.write("\n")
        except OSError:
            self._video.close()
            raise
        self._writer = threading.Thread(target=self._run, name="rec-writer",
                                        daemon=True)
        self._writer.start()

    @staticmethod
    def _ends_mid_line(path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                if f.tell() == 0:
                    return False
                f.seek(-1, os.SEEK_END)
                return f.read(1) != b"\n"
        except FileNotFoundError:
            return False

    # -- the frame loop's side ------------------------------------------------ #
    def offer(self, pre: bytes, jpeg: bytes, t_send_ms: float) -> None:
        """Queue one frame: `pre` is its header part as sent. A lock and a
        deque append -- never touches the disk, never raises."""
        self.queue.put(_Frame(pre, jpeg, int(round(t_send_ms * 1e6)), time.time()))

    @property
    def dropped(self) -> int:
        return self.queue.dropped

    # -- the writer thread ---------------------------------------------------- #
    def _run(self) -> None:
        inf = float("inf")
        now = time.monotonic()
        next_flush = now + self._flush_s
        next_fsync = now + self._fsync_s if self._fsync_s > 0 else inf
        next_disk = now + self._disk_check_s if self._disk_check_s > 0 else inf
        while True:
            items = self.queue.take_all(timeout=min(self._flush_s, 0.25))
            for it in items:
                self._write(it)
            now = time.monotonic()
            if self._finish.is_set() and not items and not len(self.queue):
                break
            try:
                if now >= next_fsync:
                    self._sync(fsync=True)
                    next_fsync = now + self._fsync_s
                    next_flush = now + self._flush_s
                elif now >= next_flush:
                    self._sync(fsync=False)
                    next_flush = now + self._flush_s
            except Exception as e:              # noqa: BLE001
                self._error(e)
            if now >= next_disk:
                next_disk = now + self._disk_check_s
                self._check_disk()
            if self._max_s > 0 and now - self.t_start >= self._max_s:
                self._guard("max duration")
        try:
            self._sync(fsync=True)
        except Exception as e:                  # noqa: BLE001
            self._error(e)

    def _write(self, fr: _Frame) -> None:
        try:
            # Where the file really ends, asked of the file: after a write
            # that failed partway, the bytes that did land are orphaned,
            # never pointed at (recorder/format.py, SessionWriter._frame).
            off = os.fstat(self._video.fileno()).st_size
            _write_all(self._video, fr.jpeg)
            # After the bytes, so an index line never points at data that is
            # not there yet.
            self._index.write(_line({
                "tm": fr.tm, "tw": round(fr.tw, 6), "off": off,
                "len": len(fr.jpeg), "crc": zlib.crc32(fr.jpeg),
                "pre": [_enc(fr.pre)],
            }))
        except Exception as e:                  # noqa: BLE001
            # A full or failing card must not kill the thread: the queue
            # would then fill and drop everything.
            self._error(e)
            self._consecutive += 1
            if self._consecutive >= MAX_WRITE_ERRORS:
                self._guard("write errors")
            return
        self._consecutive = 0
        self.written += 1
        self.nbytes += len(fr.jpeg)

    def _sync(self, fsync: bool) -> None:
        # Video before index, so the index never gets ahead on the disk.
        for f in (self._video, self._index):
            f.flush()
            if fsync:
                os.fsync(f.fileno())

    def _error(self, e: Exception) -> None:
        self.write_errors += 1
        self.last_error = repr(e)
        if self.write_errors <= 3 or self.write_errors % 500 == 0:
            log(f"[rec] {self.name}: write failed ({self.write_errors}): {e!r}")

    def _check_disk(self) -> None:
        try:
            free = self._free(self.path)
        except OSError:
            return
        if free < self._min_free:
            log(f"[rec] {self.name}: only {free / 1e6:.0f} MB free; stopping")
            self._guard("low disk")

    def _guard(self, reason: str) -> None:
        if not self._guarded:
            self._guarded = True
            self._on_guard(self, reason)

    # -- closing -------------------------------------------------------------- #
    def close(self, reason: str, control: Optional[dict] = None,
              timeout: float = 15.0) -> dict[str, Any]:
        """Write out what is queued, fsync, and finalise meta.json. Frames
        offered from here on are counted as `late`, not written."""
        self._finish.set()
        self.queue.close()                      # and wakes the writer
        self._writer.join(timeout)
        finished = not self._writer.is_alive()
        if finished:
            for f in (self._video, self._index):
                try:
                    f.flush()
                    os.fsync(f.fileno())
                    f.close()
                except Exception:               # noqa: BLE001
                    pass                        # a dead card; nothing to save
        else:
            log(f"[rec] {self.name}: the writer did not finish within "
                f"{timeout:g} s (the disk is stalled); leaving its files open")
        tw1, tm1 = time.time(), self._now_ns()
        seg = self.segment
        seg.update({
            "ended": _iso(tw1),
            "end": {"tm": tm1, "tw": round(tw1, 6)},
            "duration_s": round((tm1 - seg["start"]["tm"]) / 1e9, 3),
            "stop_reason": reason,
            "control_stop": control,
            "written": self.written,
            "bytes": self.nbytes,
            "dropped": self.queue.dropped,
            "late": self.queue.late,
            "write_errors": self.write_errors,
            "last_write_error": self.last_error,
            "writer_finished": finished,
        })
        segs = self.meta["segments"]

        def total(key: str) -> int:
            return sum(int(s.get(key) or 0) for s in segs)

        summary = {
            "written": {self.cam: total("written")},
            "written_mb": round(total("bytes") / 1e6, 2),
            "dropped": {self.cam: total("dropped")},
            "late": total("late"),
            "write_errors": total("write_errors"),
            "last_write_error": self.last_error,
        }
        self.meta.update({
            "complete": finished,
            "stopped": seg["ended"],
            "end": seg["end"],
            "duration_s": round(sum(float(s.get("duration_s") or 0) for s in segs), 3),
            "stop_reason": reason,
            "summary": summary,
        })
        try:
            write_meta(self.path, self.meta)
        except OSError as e:
            log(f"[rec] {self.name}: could not write {META}: {e!r}")
        return summary


class LocalRecorder:
    """Which session this camera is recording, if any.

    want() is driven by the recorder's control messages (control.py) and is
    idempotent: it is called with the same state every second. The frame
    loop reads `active` -- one attribute -- and offers to whatever it finds."""

    def __init__(self, root: Path, cam: str, *, now_ns: Callable[[], int],
                 queue_mb: float = 32.0, min_free_mb: float = 1000.0,
                 max_s: float = 10800.0, fsync_s: float = 5.0,
                 flush_s: float = 1.0, disk_check_s: float = 10.0,
                 settings: Optional[dict] = None,
                 free: Callable[[Path], int] = disk_free) -> None:
        self.root = Path(root)
        self.cam = cam
        self.active: Optional[Session] = None
        self.refused: dict[str, str] = {}
        """Sessions this camera will not (re)start, and why."""
        self.last: Optional[dict[str, Any]] = None
        """Summary of the most recently stopped session."""
        self._now_ns = now_ns
        self._queue_bytes = int(queue_mb * 1e6)
        self._min_free = int(min_free_mb * 1e6)
        self._max_s = max_s
        self._fsync_s = fsync_s
        self._flush_s = flush_s
        self._disk_check_s = disk_check_s
        self._settings = settings or {}
        self._free = free
        self._lock = threading.Lock()
        self._bad: set[str] = set()

    def want(self, name: Optional[str], control: Optional[dict] = None) -> None:
        """Be recording `name`, or nothing if it is None."""
        with self._lock:
            if name is not None and not valid_name(name):
                key = repr(name)[:120]
                if key not in self._bad:
                    self._bad.add(key)
                    log(f"[rec] ignoring session name {key}: not a safe "
                        "directory name")
                return
            cur = self.active
            if cur is not None and cur.name == name:
                return
            if cur is not None:
                self._stop_locked("superseded" if name else "recorder stopped",
                                  control)
            if name is None or name in self.refused:
                return
            self._start_locked(name, control)

    def shutdown(self, reason: str = "shutdown") -> None:
        with self._lock:
            if self.active is not None:
                self._stop_locked(reason, None)

    def _start_locked(self, name: str, control: Optional[dict]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            free = self._free(self.root)
        except OSError as e:
            self._refuse(name, f"cannot use {self.root}: {e}")
            return
        if free < self._min_free:
            self._refuse(name, f"low disk: {free / 1e6:.0f} MB free in "
                               f"{self.root}, --rec-min-free-mb is "
                               f"{self._min_free / 1e6:g}")
            return
        segs = read_meta(self.root / name).get("segments") or []
        prev = segs[-1].get("stop_reason") if segs and isinstance(segs[-1], dict) else None
        if prev in GUARDS:
            self._refuse(name, f"this camera already stopped it ({prev})")
            return
        try:
            s = Session(self.root, name, self.cam, now_ns=self._now_ns,
                        queue_bytes=self._queue_bytes, flush_s=self._flush_s,
                        fsync_s=self._fsync_s, disk_check_s=self._disk_check_s,
                        min_free_bytes=self._min_free, max_s=self._max_s,
                        settings=self._settings, control=control,
                        on_guard=self._on_guard, free=self._free)
        except OSError as e:
            self._refuse(name, f"cannot open it: {e}")
            return
        self.active = s                         # the frame loop starts offering
        log(f"[rec] recording {s.path}" + (" (resumed)" if s.resumed else ""))

    def _refuse(self, name: str, why: str) -> None:
        self.refused[name] = why
        log(f"[rec] not recording {name}: {why}")

    def _stop_locked(self, reason: str, control: Optional[dict]) -> None:
        s = self.active
        if s is None:
            return
        self.active = None                      # the frame loop stops offering
        summary = s.close(reason, control)
        self.last = {"session": s.name, "path": str(s.path),
                     "stop_reason": reason, **summary}
        log(f"[rec] stopped {s.name} ({reason}): {s.written} frames, "
            f"{s.nbytes / 1e6:.1f} MB, {s.queue.dropped} dropped, "
            f"{s.write_errors} write errors")

    def _on_guard(self, sess: Session, reason: str) -> None:
        # Called on the session's writer thread, which close() joins: hand off.
        def go() -> None:
            with self._lock:
                if self.active is sess:
                    self.refused[sess.name] = reason
                    self._stop_locked(reason, None)

        threading.Thread(target=go, name="rec-guard", daemon=True).start()
