#!/usr/bin/env python3
"""
A camera's own recording, played in place of the Jetson's copy of it.

A camera run with CAM_RECORD=true (vision/cam-sender, localrec.py) keeps
every frame it sends on its own SD card, under the Jetson session's name, in
this package's session format. The Jetson's copy lost whatever Wi-Fi lost;
the camera's copy did not. To play it inside a Jetson session (`--cam-copy`
on recorder.play and recorder.mix), its frames have to be put on the Jetson's
clock:

  * A copy's `tm` is the header's `t_send_ms` in ns, on the *camera's* clock.
  * The frames that reached the Jetson are in both copies, with the same
    header bytes, so they are matched on (`seq`, `t_send_ms`). Over those
    frames, `jetson tm - copy tm` is the clock offset plus a delay that is
    never negative; its minimum is the offset (sync.py's rule, and the one
    april-detect applies live).
  * Every frame of the copy then goes out at `copy tm + offset`: when the
    camera sent it, plus the least delay seen. Frames the Jetson lost are
    filled in; the Wi-Fi jitter of the Jetson's copy is gone, so the
    detectors' clock estimators see a steadier stream than they did live.
  * A camera that rebooted mid-session starts its clock again. The copy is
    split where its clock runs backwards (export.timeline's rule) and each
    run gets its own offset. A run with no frame in common with the Jetson's
    copy cannot be placed, and is left out with a warning.
  * Only frames inside the Jetson session's start and end are played, so
    the camera's copy ends with the rest of the session.

The JPEG bytes and the header are the camera's own, unchanged. A copy holds
no `up` stamp, so its frames are scheduled on the mapped `tm`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .format import Event, Session, frame_header, num, read_meta
from .log import log
from .sync import REBOOT_S

WRITER = "cam-sender"
"""`writer` in a camera recording's meta.json."""

Key = tuple[int, float]
"""(seq, t_send_ms): one frame, the same in both copies."""


def is_copy(path: Path) -> bool:
    return path.is_dir() and read_meta(path).get("writer") == WRITER


def find_copies(paths: list[str] | list[Path]) -> list[Path]:
    """Camera recordings at or directly under each path: a recording itself
    (`pi/20261110T000213Z`), a directory of them (`pi/`), or a Jetson session
    holding them (`data/<s>/cam1/`)."""
    out: list[Path] = []
    for p in map(Path, paths):
        if not p.is_dir():
            raise FileNotFoundError(f"no such directory: {p}")
        found = [p] if is_copy(p) else sorted(c for c in p.iterdir() if is_copy(c))
        if not found:
            log(f"[copy] no camera recording (meta.json writer {WRITER!r}) "
                f"at or under {p}")
        for c in found:
            if c.resolve() not in {o.resolve() for o in out}:
                out.append(c)
    return out


def _key(ev: Event) -> Optional[Key]:
    h = frame_header(ev.pre)
    seq, ts = num(h.get("seq")), num(h.get("t_send_ms"))
    return None if seq is None or ts is None else (int(seq), ts)


def _runs(sess: Session, cam: str) -> Iterator[tuple[int, Event]]:
    """(run number, frame): a new run where the copy's clock runs backwards."""
    run, high = -1, None
    for ev in sess.frames(cam):
        if high is None or ev.tm < high - REBOOT_S * 1e9:
            run += 1
            high = ev.tm
        high = max(high, ev.tm)
        yield run, ev


@dataclass
class CamCopy:
    """One camera's own recording, placed on a Jetson session's clock."""

    copy: Session
    cam: str
    """Its name in the copy (the file name: `cam-pi`)."""
    as_cam: str
    """The Jetson's name for it (`cam1`)."""
    offsets: list[Optional[int]] = field(default_factory=list)
    """Per run of the copy's clock: Jetson tm - copy tm, ns; None if the run
    has no frame in common with the Jetson's copy."""
    window: tuple[Optional[int], Optional[int]] = (None, None)
    """The Jetson session's start and end `tm`."""
    wall: Optional[tuple[int, float]] = None
    """A (tm, tw) pair of the Jetson's, to give each frame a Jetson wall time."""
    n_common: int = 0
    n_jetson: int = 0

    def frames(self) -> Iterator[Event]:
        """The copy's frames as the Jetson's camera `as_cam`, in time order."""
        lo, hi = self.window
        last: Optional[int] = None
        for run, ev in _runs(self.copy, self.cam):
            off = self.offsets[run] if run < len(self.offsets) else None
            if off is None:
                continue
            tm = ev.tm + off
            if (lo is not None and tm < lo) or (hi is not None and tm > hi):
                continue
            if last is not None and tm <= last:
                continue                # runs that overlap after a reboot
            last = tm
            rec = dict(ev.rec, tm=tm)
            if self.wall is not None:
                rec["tw"] = round(self.wall[1] + (tm - self.wall[0]) / 1e9, 6)
            yield Event(tm, "frame", self.as_cam, rec)

    def jpeg(self, ev: Event) -> bytes:
        """A frame from frames(), read from the copy, CRC checked."""
        return self.copy.jpeg(Event(ev.tm, "frame", self.cam, ev.rec))

    def count(self) -> int:
        return sum(1 for _ in self.frames())

    def close(self) -> None:
        self.copy.close()


def _place(copy: Session, cam: str, jet: dict[Key, int]) -> tuple[list[Optional[int]], int]:
    """Per-run offsets of `cam` in `copy` against the Jetson frames `jet`
    ({key: tm}), and how many frames the two have in common."""
    best: list[Optional[int]] = []
    common = 0
    for run, ev in _runs(copy, cam):
        while len(best) <= run:
            best.append(None)
        k = _key(ev)
        tm = jet.get(k) if k is not None else None
        if tm is None:
            continue
        common += 1
        d = tm - ev.tm
        if best[run] is None or d < best[run]:
            best[run] = d
    return best, common


def attach(sess: Session, copies: list[Path], cameras: list[str],
           tag: str = "[copy]") -> dict[str, CamCopy]:
    """The camera recordings among `copies` that belong to `sess`, each
    matched to the Jetson camera (of `cameras`) it has the most frames in
    common with. {Jetson camera: CamCopy}."""
    mine = [c for c in copies
            if (read_meta(c).get("name") or c.name) == sess.name]
    if not mine:
        return {}
    jet: dict[str, dict[Key, int]] = {}
    for cam in cameras:
        keys: dict[Key, int] = {}
        for ev in sess.frames(cam):
            k = _key(ev)
            if k is not None:
                keys.setdefault(k, ev.tm)
        jet[cam] = keys
    start, end = sess.meta.get("start") or {}, sess.meta.get("end") or {}
    lo, hi = start.get("tm"), end.get("tm")
    wall = ((lo, float(start["tw"])) if isinstance(lo, int)
            and num(start.get("tw")) is not None else None)

    out: dict[str, CamCopy] = {}
    for path in mine:
        copy = Session(path)
        keep = False
        for ccam in copy.cameras():
            placed = [(*_place(copy, ccam, keys), cam) for cam, keys in jet.items()
                      if cam not in out]
            placed = [p for p in placed if p[1] > 0]
            if not placed:
                log(f"{tag} {path}: {ccam} has no frame in common with "
                    f"{', '.join(cameras) or 'any camera'} of {sess.name}; "
                    "it cannot be put on the Jetson's clock")
                continue
            offsets, common, cam = max(placed, key=lambda p: p[1])
            cc = CamCopy(copy, ccam, cam, offsets,
                         (lo if isinstance(lo, int) else None,
                          hi if isinstance(hi, int) else None),
                         wall, common, len(jet[cam]))
            lost = sum(o is None for o in offsets)
            if lost:
                log(f"{tag} {path}: {lost} of {len(offsets)} runs of {ccam}'s "
                    "clock have no frame in common with the Jetson's copy; "
                    "left out")
            log(f"{tag} {sess.name} {cam}: playing {ccam}'s own copy ({path}): "
                f"{cc.count()} frames, against {cc.n_jetson} in the Jetson's "
                f"({common} in common, {len(offsets)} run(s))")
            out[cam] = cc
            keep = True
        if not keep:
            copy.close()
    return out
