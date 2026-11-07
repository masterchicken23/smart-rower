#!/usr/bin/env python3
"""
Between the receivers and the disk: a byte-bounded queue and one writer thread.

The receivers only ever append to the queue, so nothing the disk does -- an SD
card pausing for wear levelling, an fsync -- can delay a stamp. When the queue
is full the *oldest* waiting item is dropped and counted per stream, the same
policy as every queue in this repo (funnel/recorder.py, pose_stream.py): a
recording with a counted hole is better than a receiver that blocks.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from . import log
from .format import Item, SessionWriter


class WriteQueue:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(1, int(max_bytes))
        self._cv = threading.Condition()
        self._q: deque = deque()
        self.nbytes = 0
        self.dropped: dict[str, int] = {}

    def put(self, item: Item) -> None:
        """Never blocks for longer than a lock acquire, never raises."""
        size = item.size
        with self._cv:
            self._q.append((item, size))
            self.nbytes += size
            while self.nbytes > self.max_bytes and len(self._q) > 1:
                old, osize = self._q.popleft()
                self.nbytes -= osize
                self.dropped[old.stream] = self.dropped.get(old.stream, 0) + 1
            self._cv.notify()

    def take_all(self, timeout: float) -> list[Item]:
        with self._cv:
            if not self._q:
                self._cv.wait(timeout)
            items = [it for it, _ in self._q]
            self._q.clear()
            self.nbytes = 0
            return items

    def __len__(self) -> int:
        return len(self._q)


def disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


# ---- what the recording volume is formatted as --------------------------- #

FAT32 = {"vfat", "msdos", "fat", "fat32"}
EXFAT = {"exfat"}
FUSEBLK = {"fuseblk"}           # exfat-fuse or ntfs-3g on older systems


def _existing(path: Path) -> Path:
    """REC_DIR may not exist yet; its nearest existing ancestor is on the
    same volume."""
    p = Path(os.path.abspath(path))
    while not p.exists() and p.parent != p:
        p = p.parent
    return Path(os.path.realpath(p))


def fs_type_from_mounts(path: str, mounts: str) -> Optional[str]:
    """The filesystem `path` lives on, from /proc/self/mounts text: the
    longest mount point that contains it. (Inside a container a bind mount
    reports the host filesystem's type, which is the one that matters.)"""
    best, kind = "", None
    for line in mounts.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        # /proc/mounts escapes space, tab, newline and backslash as \ooo.
        mnt = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
        inside = (path == mnt or mnt == "/"
                  or path.startswith(mnt.rstrip("/") + "/"))
        if inside and len(mnt) >= len(best):
            best, kind = mnt, parts[2].lower()
    return kind


def fs_type(path: Path) -> Optional[str]:
    """Filesystem type of the volume holding `path`, lower case, or None if
    it cannot be told."""
    p = _existing(path)
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/mounts", encoding="utf-8") as f:
                return fs_type_from_mounts(str(p), f.read())
        except OSError:
            return None
    if sys.platform == "win32":               # development machines
        import ctypes

        k32 = ctypes.windll.kernel32           # type: ignore[attr-defined]
        vol = ctypes.create_unicode_buffer(261)
        name = ctypes.create_unicode_buffer(261)
        if not k32.GetVolumePathNameW(str(p), vol, 261):
            return None
        if not k32.GetVolumeInformationW(vol.value, None, 0, None, None, None,
                                         name, 261):
            return None
        return name.value.lower() or None
    return None


def fs_warning(kind: Optional[str]) -> Optional[str]:
    """Why a filesystem is a poor place for a recording, or None."""
    if kind in FAT32:
        return ("the recording directory is on FAT32: a camera's video file "
                "cannot grow past 4 GB, so that camera stops recording after "
                "about 2 h at 35 KB/frame, or about 30 min at webcam frame "
                "sizes; and with no journal, a power cut can damage the "
                "filesystem rather than only lose the last few seconds. "
                "Format the drive ext4.")
    if kind in EXFAT:
        return ("the recording directory is on exFAT: with no journal, a "
                "power cut can damage the filesystem rather than only lose "
                "the last few seconds. Format the drive ext4.")
    if kind in FUSEBLK:
        return ("the recording directory is on a FUSE filesystem (fuseblk: "
                "usually exFAT or NTFS through FUSE), which may not survive a "
                "power cut as ext4 does. Format the drive ext4.")
    return None


def storage_info(path: Path) -> dict[str, Any]:
    kind = fs_type(path)
    return {"dir": str(path), "fs": kind, "warning": fs_warning(kind)}


class Writer(threading.Thread):
    """Drains the queue into a SessionWriter until finish() is called and the
    queue is empty. Flushes every `flush_s`, fsyncs every `fsync_s`, and calls
    `on_low_disk` once if free space falls below `min_free_bytes`."""

    def __init__(self, files: SessionWriter, q: WriteQueue, *,
                 flush_s: float, fsync_s: float, disk_check_s: float,
                 min_free_bytes: int,
                 on_low_disk: Optional[Callable[[int], None]] = None,
                 free: Callable[[Path], int] = disk_free) -> None:
        super().__init__(name="writer", daemon=True)
        self.files = files
        self.q = q
        self.flush_s = max(flush_s, 0.05)
        self.fsync_s = fsync_s
        self.disk_check_s = disk_check_s
        self.min_free_bytes = min_free_bytes
        self.on_low_disk = on_low_disk
        self._free = free
        self._finish = threading.Event()
        self.n_errors = 0
        self.last_error: Optional[str] = None
        self.low_disk = False

    def finish(self, timeout: float = 30.0) -> None:
        """Write out whatever is queued, then stop."""
        self._finish.set()
        with self.q._cv:
            self.q._cv.notify()
        self.join(timeout)

    def run(self) -> None:
        now = time.monotonic()
        next_flush = now + self.flush_s
        next_fsync = now + self.fsync_s if self.fsync_s > 0 else float("inf")
        next_disk = now
        while True:
            items = self.q.take_all(timeout=min(self.flush_s, 0.25))
            for it in items:
                try:
                    self.files.write(it)
                except Exception as e:          # noqa: BLE001
                    # A full or failing volume must not kill the thread: the
                    # queue would then grow until it dropped everything.
                    self.n_errors += 1
                    self.last_error = repr(e)
                    if self.n_errors <= 3 or self.n_errors % 500 == 0:
                        log.error(f"[writer] write failed: {e!r}")
            now = time.monotonic()
            if self._finish.is_set() and not items and not len(self.q):
                break
            try:
                if now >= next_fsync:
                    self.files.flush(fsync=True)
                    next_fsync = now + self.fsync_s
                    next_flush = now + self.flush_s
                elif now >= next_flush:
                    self.files.flush()
                    next_flush = now + self.flush_s
            except Exception as e:              # noqa: BLE001
                self.n_errors += 1
                self.last_error = repr(e)
            if self.disk_check_s > 0 and now >= next_disk:
                next_disk = now + self.disk_check_s
                self._check_disk()
        try:
            self.files.flush(fsync=True)
        except Exception as e:                  # noqa: BLE001
            self.n_errors += 1
            self.last_error = repr(e)

    def _check_disk(self) -> None:
        if self.low_disk:
            return
        try:
            free = self._free(self.files.path)
        except OSError:
            return
        if free < self.min_free_bytes:
            self.low_disk = True
            log.error(f"[writer] only {free / 1e9:.2f} GB free; stopping the "
                      f"session")
            if self.on_low_disk is not None:
                self.on_low_disk(free)
