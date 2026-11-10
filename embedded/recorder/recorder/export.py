#!/usr/bin/env python3
"""
A session's video as MP4 (H.264), every frame shown at the instant the camera
captured it. For watching, scrubbing and sharing; a replay wants the original
JPEGs (recorder.play).

    python -m recorder.export data/20261107T143000Z
    python -m recorder.export data/<s> data/<s>/cam1          # several sessions
    python -m recorder.export data/<s> --cam cam2 --crf 23

Each MP4 goes in its session's directory, named after it:
`<session>.mp4`, or `<session>_<cam>.mp4` when the session holds more than
one camera. A camera's own recording (cam-sender --record) is a session
directory too.

Timing -- why not `ffmpeg -f mjpeg -r 15`:

  * A camera's rate is not its nominal one. It captures at ~30 fps and sends
    about every other frame, so frames are mostly 68 ms apart with some 32 ms
    steps; and frames lost on Wi-Fi leave holes in the Jetson's copy. Played
    at a fixed rate, a copy that lost 3 % of its frames runs 3 % fast, nearly
    2 s a minute.
  * Arrival times are no better: Wi-Fi delivers frames in bursts, 10-300 ms
    apart.
  * So each frame is placed at its capture time, the header's `t_ms`: the
    spacing is the camera's own, put on the recording's clock by its least
    delay, and split where the camera rebooted (sync.py's rule). Not by
    sync.fit_sender's windowed fit, which follows drift but bends when Wi-Fi
    is slow for a whole window. The MP4 is variable frame rate: a lost frame
    shows as the previous one held for the gap, and the video lasts exactly
    as long as the capture did.
  * A camera's own recording keeps `tm` on the camera's clock, which starts
    again if the camera reboots and resumes the session. Its pieces are
    joined by the wall-clock gap between them, the only clock that spans the
    reboot (and only as good as the camera's wall clock after it).

Times are kept to the millisecond. Frames that fail their CRC-32 are left
out and counted. The first frame is the video's 0; how long after the
session's start it was captured is printed, and kept in the MP4's comment.

Needs ffmpeg 5.1 or newer with libx264: on PATH, given with --ffmpeg, or the
one bundled by the imageio-ffmpeg package (`pip install imageio-ffmpeg`).
The recorder's image has none; this is a tool for a laptop.

Argparse, as the repo's other hand-run tools.
"""

from __future__ import annotations

import argparse
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .format import VIDEO_EXT, CorruptFrameError, Event, Session, frame_header, num
from .log import log
from .sync import REBOOT_S, Obs, segments

TIMEBASE = 1000
"""Ticks per second of the frame times handed to ffmpeg. The concat
demuxer's per-image default is 1/25 s, which moves frames by up to 40 ms and
lets the error build up over a session."""

MIN_FFMPEG = (5, 1)
"""`-fps_mode`, and the concat demuxer's `option` directive."""

DEFAULT_STEP_S = 1 / 15
"""How long the last frame is shown when there is no interval to go by."""


@dataclass
class Timeline:
    """A camera's frames on one continuous capture-time axis."""

    cam: str
    frames: list[tuple[float, Event]] = field(default_factory=list)
    """(capture time, s; frame), in time order."""
    pieces: int = 0
    """Pieces mapped on their own: one, plus one per camera reboot."""
    untimed: int = 0
    """Frames with no capture clock, among others that have one: left out."""
    duplicate: int = 0
    """Frames at or before the previous frame's time: left out."""


def step_s(frames: list[tuple[float, Event]] | list[tuple[float, Path]]) -> float:
    """The typical frame interval: how long the last frame is shown."""
    d = [b[0] - a[0] for a, b in zip(frames, frames[1:], strict=False)]
    return statistics.median(d) if d else DEFAULT_STEP_S


def _wall(ev: Event) -> Optional[float]:
    return num(ev.rec.get("tw"))


def _map_run(run: list[Event], tl: Timeline) -> list[tuple[float, Event]]:
    """One stretch of the recording's own clock -> [(capture s, frame)]."""
    timed: list[Event] = []
    obs: list[Obs] = []
    for ev in run:
        h = frame_header(ev.pre)
        t_ms, t_send = num(h.get("t_ms")), num(h.get("t_send_ms"))
        # Offset on t_send_ms, capture at t_ms: fit_session's choice.
        anchor = t_send if t_send is not None else t_ms
        if anchor is None:
            continue
        timed.append(ev)
        obs.append(Obs(ev.tm_arrival, anchor,
                       t_ms if t_ms is not None else anchor, None))
    if not timed:
        # A sender with no clock in its header: arrival is all there is.
        tl.pieces += 1
        return sorted(((ev.tm_arrival / 1e9, ev) for ev in run),
                      key=lambda x: x[0])
    tl.untimed += len(run) - len(timed)
    # Split where the camera's clock runs backwards (it rebooted), by the
    # report's rule. Within a piece the camera's own clock is the frame
    # spacing, moved by one offset: the least (arrival - camera clock). Not
    # sync.fit_sender's windowed fit: a window with no frame through quickly
    # (a spell of slow Wi-Fi) bends that fit, and with it the frame rate.
    by_obs = {id(o): ev for o, ev in zip(obs, timed, strict=True)}
    out: list[tuple[float, Event]] = []
    for piece in segments(sorted(obs, key=lambda o: o.tm)):
        off = min(o.tm / 1e9 - o.anchor_ms / 1000.0 for o in piece)
        out += [(o.cap_ms / 1000.0 + off, by_obs[id(o)]) for o in piece]
        tl.pieces += 1
    return sorted(out, key=lambda x: x[0])


def timeline(sess: Session, cam: str) -> Timeline:
    """Every indexed frame of `cam`, at its capture time. Reads the index,
    not the video."""
    tl = Timeline(cam)
    # Stretches of the recording's own clock (`tm`). On the Jetson it never
    # starts again; in a camera's own recording it does when the camera
    # reboots and resumes the session.
    runs: list[list[Event]] = []
    high = 0
    for ev in sess.frames(cam):
        t = ev.tm_arrival
        if not runs or t < high - REBOOT_S * 1e9:
            runs.append([])
            high = t
        runs[-1].append(ev)
        high = max(high, t)

    out: list[tuple[float, Event]] = []
    for run in runs:
        mapped = _map_run(run, tl)
        if out and mapped:
            # Join on the wall clock, at least one frame interval apart.
            w0, w1 = _wall(out[-1][1]), _wall(mapped[0][1])
            gap = w1 - w0 if w0 is not None and w1 is not None else 0.0
            shift = out[-1][0] + max(gap, step_s(out)) - mapped[0][0]
            mapped = [(t + shift, ev) for t, ev in mapped]
        out.extend(mapped)

    for t, ev in sorted(out, key=lambda x: x[0]):
        if tl.frames and t <= tl.frames[-1][0]:
            tl.duplicate += 1
            continue
        tl.frames.append((t, ev))
    return tl


# ---- ffmpeg ------------------------------------------------------------- #

def find_ffmpeg(given: Optional[str] = None) -> Optional[str]:
    """--ffmpeg, else ffmpeg on PATH, else imageio-ffmpeg's; None if none."""
    if given:
        return given
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # optional; only for this tool
    except ImportError:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError:
        return None


def ffmpeg_version(ff: str) -> Optional[tuple[int, int]]:
    """(major, minor), or None for a build that does not say (a git
    snapshot: assume it is recent)."""
    r = subprocess.run([ff, "-hide_banner", "-version"], capture_output=True,
                       text=True, check=True)
    m = re.match(r"ffmpeg version n?(\d+)\.(\d+)", r.stdout)
    return (int(m[1]), int(m[2])) if m else None


def write_frames(sess: Session, tl: Timeline, folder: Path
                 ) -> tuple[list[tuple[float, Path]], int]:
    """Each frame's JPEG to its own file in `folder` -> ([(time, file)],
    frames that failed their CRC-32 and were left out)."""
    kept: list[tuple[float, Path]] = []
    corrupt = 0
    for i, (t, ev) in enumerate(tl.frames):
        try:
            data = sess.jpeg(ev)
        except CorruptFrameError:
            corrupt += 1
            continue
        p = folder / f"{i:07d}.jpg"
        p.write_bytes(data)
        kept.append((t, p))
    return kept, corrupt


def concat_list(frames: list[tuple[float, Path]], last_s: float) -> str:
    """An ffconcat script showing each file from its time until the next one,
    and the last for `last_s`. Durations are differences of times rounded
    from the first frame, so rounding never builds up."""
    t0 = frames[0][0]
    us = [round((t - t0) * 1e6) for t, _ in frames]
    us.append(us[-1] + max(1, round(last_s * 1e6)))
    lines = ["ffconcat version 1.0"]
    for i, (_t, p) in enumerate(frames):
        q = p.as_posix().replace("'", "'\\''")
        lines += [f"file '{q}'",
                  # the image's timebase; `option` needs -safe 0
                  f"option framerate {TIMEBASE}",
                  f"duration {(us[i + 1] - us[i]) / 1e6:.6f}"]
    return "\n".join(lines) + "\n"


def ffmpeg_cmd(ff: str, script: Path, out: Path, crf: int, preset: str,
               comment: str) -> list[str]:
    cmd = [ff, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(script),
           "-fps_mode", "passthrough",          # the frames' own times
           # JPEG is full range; most players assume limited ("tv").
           "-vf", "scale=out_range=tv,format=yuv420p", "-color_range", "tv",
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
           "-movflags", "+faststart", "-metadata", f"comment={comment}",
           str(out)]
    if sys.stderr.isatty():
        cmd.insert(1, "-stats")
    return cmd


# ---- one camera --------------------------------------------------------- #

@dataclass
class Result:
    cam: str
    out: Path
    frames: int
    span_s: float
    steps_ms: tuple[float, float, float]
    """Frame interval: min, median, max."""
    first_s: Optional[float]
    """First frame's capture after the session's start."""
    corrupt: int
    untimed: int
    duplicate: int
    pieces: int

    def render(self) -> str:
        fps = (self.frames - 1) / self.span_s if self.span_s > 0 else 0.0
        lo, mid, hi = self.steps_ms
        s = (f"{self.cam} -> {self.out}: {self.frames} frames, "
             f"{self.span_s:.2f} s, {fps:.2f} fps "
             f"(intervals {lo:.0f} / {mid:.0f} / {hi:.0f} ms min/median/max)")
        if self.first_s is not None:
            s += f"; first frame {self.first_s:+.3f} s from session start"
        left = {"failed CRC": self.corrupt, "untimed": self.untimed,
                "duplicate": self.duplicate}
        left_out = ", ".join(f"{n} {k}" for k, n in left.items() if n)
        if left_out:
            s += f"; left out: {left_out}"
        if self.pieces > 1:
            s += f"; {self.pieces} clock pieces (camera reboots) joined"
        return s


def out_path(sess: Session, cam: str) -> Path:
    name = sess.path.resolve().name
    if len(sess.cameras()) > 1:
        name = f"{name}_{cam}"
    return sess.path / f"{name}.mp4"


def export(sess: Session, cam: str, out: Path, ff: str, crf: int = 18,
           preset: str = "slow") -> Optional[Result]:
    """Write `cam`'s video to `out`. None if it has no readable frames."""
    tl = timeline(sess, cam)
    if not tl.frames:
        return None
    start = sess.meta.get("start")
    start_tm = num(start.get("tm")) if isinstance(start, dict) else None
    part = out.with_name(out.stem + ".part.mp4")
    with tempfile.TemporaryDirectory(prefix="recorder-export-") as tmp:
        frames, corrupt = write_frames(sess, tl, Path(tmp))
        if not frames:
            return None
        t0 = frames[0][0]
        first_s = t0 - start_tm / 1e9 if start_tm is not None else None
        script = Path(tmp) / "frames.ffconcat"
        script.write_text(concat_list(frames, step_s(frames)), encoding="utf-8")
        comment = (f"{sess.path.resolve().name} {cam}: frames at capture time"
                   + (f"; first frame {first_s:+.3f} s from session start"
                      if first_s is not None else ""))
        try:
            subprocess.run(ffmpeg_cmd(ff, script, part, crf, preset, comment),
                           check=True)
            part.replace(out)
        finally:
            part.unlink(missing_ok=True)
    d = [(b[0] - a[0]) * 1000
         for a, b in zip(frames, frames[1:], strict=False)] or [0.0]
    return Result(cam, out, len(frames), frames[-1][0] - t0,
                  (min(d), statistics.median(d), max(d)), first_s, corrupt,
                  tl.untimed, tl.duplicate, tl.pieces)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Export a session's video as MP4, each frame at its "
                    "capture time.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("sessions", nargs="+", metavar="session",
                   help="session directory: the Jetson's, or a camera's own")
    p.add_argument("--cam", action="append",
                   help="only this camera (repeatable); default every one")
    p.add_argument("--crf", type=int, default=18,
                   help="x264 quality: lower is better and larger; 18 looks "
                        "like the original")
    p.add_argument("--preset", default="slow",
                   help="x264 preset: slower makes smaller files")
    p.add_argument("--ffmpeg", help="ffmpeg to use (default: PATH, then "
                                    "imageio-ffmpeg's)")
    args = p.parse_args(argv)

    ff = find_ffmpeg(args.ffmpeg)
    if ff is None:
        log("[export] no ffmpeg: put ffmpeg 5.1+ on PATH, pass --ffmpeg, or "
            "pip install imageio-ffmpeg")
        return 2
    try:
        ver = ffmpeg_version(ff)
    except (OSError, subprocess.CalledProcessError) as e:
        log(f"[export] cannot run {ff}: {e}")
        return 2
    if ver is not None and ver < MIN_FFMPEG:
        log(f"[export] {ff} is ffmpeg {ver[0]}.{ver[1]}; this needs "
            f"{MIN_FFMPEG[0]}.{MIN_FFMPEG[1]} or newer")
        return 2

    rc = 0
    for path in args.sessions:
        try:
            sess = Session(path)
        except FileNotFoundError as e:
            log(f"[export] {e}")
            rc = 1
            continue
        with sess:
            cams = [c for c in sess.cameras()
                    if not args.cam or c in args.cam]
            if not cams:
                log(f"[export] {sess.path}: no camera to export")
                rc = 1
            for cam in cams:
                out = out_path(sess, cam)
                log(f"[export] {sess.path / (cam + VIDEO_EXT)} -> {out}")
                try:
                    res = export(sess, cam, out, ff, args.crf, args.preset)
                except subprocess.CalledProcessError as e:
                    log(f"[export] {cam}: ffmpeg failed ({e.returncode})")
                    rc = 1
                    continue
                if res is None:
                    log(f"[export] {cam}: no readable frames")
                    rc = 1
                else:
                    print(res.render(), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
