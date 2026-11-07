#!/usr/bin/env python3
"""
The session format: how a recording is laid out on disk. Written by the
recorder, read back by the report and the player.

    <REC_DIR>/<YYYYMMDDTHHMMSSZ>[_label]/
        meta.json          start/end clock anchors, config, sources, summary
        mqtt.jsonl         every MQTT message, in arrival order
        apriltag.jsonl     every detector message (apriltag, apriltag_meta)
        cam1.mjpeg         the camera's JPEGs, byte for byte, concatenated
        cam1.idx.jsonl     one line per frame: where it is, when it came

Every line carries two stamps taken back to back in the receiving thread:

    tm   time.perf_counter_ns(). On Linux that is CLOCK_MONOTONIC, which is
         host-wide -- the same clock the funnel's `t_recv` and april-detect's
         stamps are on, in every container. The session's timebase: immune to
         wall-clock steps, and what the player schedules by.
    tw   time.time(), for absolute reference.

Payloads are stored verbatim -- the sensor's `t_ms` and `seq`, the camera's
header with `t_ms`/`t_send_ms` -- because those are the *capture* instants on
the senders' own clocks, and arrival times carry Wi-Fi and broker jitter.
A payload is a JSON string when it is valid UTF-8 and `{"b64": ...}` when not.

Each file is written by one receiver thread, so each is in `tm` order on its
own and readers merge them with heapq.

What a damaged file costs, which is never the whole session:

  * A torn or unreadable line in any .jsonl costs that one line; newlines
    resynchronise the reader.
  * Frames are independent JPEGs with no container to finalise, so a session
    that was never stopped plays up to its last flushed frame.
  * A frame's offset is where the .mjpeg actually ended when it was written,
    read from the file rather than counted, so a write that fails halfway
    leaves orphaned bytes nobody points at -- not a skew in every later
    offset. That frame gets no index line.
  * An index entry pointing past the end of the .mjpeg (a cut-off tail, or a
    damaged line) is skipped, and reading carries on with the next.
  * Every index entry carries the frame's CRC-32, so a frame whose bytes were
    damaged on the disk is detected on reading (CorruptFrameError) instead
    of being replayed as garbage.
"""

from __future__ import annotations

import base64
import heapq
import json
import os
import re
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Optional, TextIO

FORMAT_VERSION = 1
META = "meta.json"
MQTT = "mqtt.jsonl"
APRILTAG = "apriltag.jsonl"
VIDEO_EXT = ".mjpeg"
INDEX_EXT = ".idx.jsonl"

TEE_MARK = b"tee1"
"""First frame of a message from april-detect's raw frame tee:
[b"tee1", {"t_mono", "t_wall"}, *the camera's own parts]."""

U32 = 2 ** 32


class CorruptFrameError(Exception):
    """A frame's bytes are missing or fail their CRC-32."""


def stamp() -> tuple[int, float]:
    """(monotonic ns, wall seconds), taken back to back. Call it first, before
    touching the message, so the stamp is the moment of arrival."""
    return time.perf_counter_ns(), time.time()


# ---- session names ------------------------------------------------------ #

_LABEL_BAD = re.compile(r"[^A-Za-z0-9._-]+")


def clean_label(label: str) -> str:
    """A label is appended to the directory name, so keep it filename-safe."""
    return _LABEL_BAD.sub("-", label.strip()).strip("-.")[:48]


def session_name(now: Optional[datetime] = None, label: str = "") -> str:
    """`20261107T143000Z`, or `20261107T143000Z_pause-drill` -- the format of
    the funnel's own recorder sessions, plus an optional label."""
    now = now or datetime.now(UTC)
    name = now.strftime("%Y%m%dT%H%M%SZ")
    lab = clean_label(label)
    return f"{name}_{lab}" if lab else name


def create_session_dir(root: Path, label: str = "",
                       now: Optional[datetime] = None) -> Path:
    """Create a new, empty session directory. Never reuses one: two sessions
    started in the same second get `-2`, `-3`, ... rather than sharing files."""
    root.mkdir(parents=True, exist_ok=True)
    base = session_name(now, label)
    for i in range(1, 1000):
        path = root / (base if i == 1 else f"{base}-{i}")
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise FileExistsError(f"no free session name for {base} in {root}")


# ---- payloads ----------------------------------------------------------- #

def enc_bytes(b: bytes) -> Any:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(b).decode("ascii")}


def dec_bytes(v: Any) -> bytes:
    if isinstance(v, str):
        return v.encode("utf-8")
    if isinstance(v, dict) and isinstance(v.get("b64"), str):
        return base64.b64decode(v["b64"])
    return b""


def json_obj(b: bytes) -> Optional[dict]:
    """A payload as a JSON object, or None if it is not one."""
    try:
        obj = json.loads(b)
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def frame_header(pre: list[bytes]) -> dict:
    """The camera header from the frames before the JPEG, as SENDER.md defines
    it: imageZMQ's `{"msg": {...}}`, a bare header dict, a JSON string in
    `msg`, or a plain name. A minimal port of april_detect/wire.py; {} when
    there is nothing to read."""
    if not pre:
        return {}
    first = pre[0]
    if first[:1] != b"{":
        return {"cam": first.decode("utf-8", "replace")}
    md = json_obj(first)
    if md is None:
        return {}
    msg: Any = md.get("msg", md)
    if isinstance(msg, str):
        s = msg.strip()
        if s.startswith("{"):
            try:
                msg = json.loads(s)
            except ValueError:
                return {"cam": msg}
        else:
            return {"cam": msg}
    return msg if isinstance(msg, dict) else {}


def split_tee(parts: list[bytes]) -> tuple[list[bytes], Optional[tuple[int, float]]]:
    """A received message -> (the camera's own parts, the tee's arrival stamp
    as (monotonic ns, wall s)). A message straight from a camera passes
    through with no stamp."""
    if len(parts) >= 3 and parts[0] == TEE_MARK:
        st = json_obj(parts[1]) or {}
        mono, wall = num(st.get("t_mono")), num(st.get("t_wall"))
        up = (int(round(mono * 1e9)), wall) if mono is not None and wall is not None else None
        return list(parts[2:]), up
    return list(parts), None


# ---- items handed from the receivers to the writer ---------------------- #

@dataclass(slots=True)
class MqttMsg:
    tm: int
    tw: float
    topic: str
    qos: int
    retain: bool
    payload: bytes

    @property
    def stream(self) -> str:
        return "mqtt"

    @property
    def size(self) -> int:
        return len(self.payload) + len(self.topic) + 96


@dataclass(slots=True)
class AprilMsg:
    tm: int
    tw: float
    topic: bytes
    payload: bytes

    @property
    def stream(self) -> str:
        return "apriltag"

    @property
    def size(self) -> int:
        return len(self.payload) + 96


@dataclass(slots=True)
class FrameMsg:
    cam: str
    tm: int
    tw: float
    pre: list[bytes]
    jpeg: bytes
    up: Optional[tuple[int, float]] = None

    @property
    def stream(self) -> str:
        return self.cam

    @property
    def size(self) -> int:
        return len(self.jpeg) + sum(len(p) for p in self.pre) + 128


Item = MqttMsg | AprilMsg | FrameMsg


# ---- writing ------------------------------------------------------------ #

def _line(obj: dict) -> str:
    return json.dumps(obj, separators=(",", ":")) + "\n"


def _write_all(f: Any, data: bytes) -> None:
    """Unbuffered write, looping over short writes."""
    mv = memoryview(data)
    while mv:
        n = f.write(mv)
        if not n:
            raise OSError("write made no progress")
        mv = mv[n:]


class SessionWriter:
    """The open files of one session. Used by the writer thread only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._text: dict[str, TextIO] = {}
        self._video: dict[str, Any] = {}
        self.written: dict[str, int] = {}
        """Items written, per stream (`mqtt`, `apriltag`, or a camera)."""
        self.nbytes = 0

    def write(self, item: Item) -> None:
        if isinstance(item, FrameMsg):
            self._frame(item)
        elif isinstance(item, MqttMsg):
            self._put(MQTT, {"tm": item.tm, "tw": round(item.tw, 6),
                             "topic": item.topic, "qos": item.qos,
                             "retain": item.retain, "p": enc_bytes(item.payload)})
        else:
            self._put(APRILTAG, {"tm": item.tm, "tw": round(item.tw, 6),
                                 "topic": item.topic.decode("utf-8", "replace"),
                                 "p": enc_bytes(item.payload)})
        self.written[item.stream] = self.written.get(item.stream, 0) + 1

    def _put(self, name: str, obj: dict) -> None:
        f = self._text.get(name)
        if f is None:
            f = open(self.path / name, "a", encoding="utf-8")  # noqa: SIM115
            self._text[name] = f
        s = _line(obj)
        f.write(s)
        self.nbytes += len(s)

    def _frame(self, fr: FrameMsg) -> None:
        vf = self._video.get(fr.cam)
        if vf is None:
            # Unbuffered and append-only: each frame goes to the OS in one
            # write, at the end of the file, before its index line exists.
            vf = open(self.path / (fr.cam + VIDEO_EXT), "ab", buffering=0)  # noqa: SIM115
            self._video[fr.cam] = vf
        # Where the file really ends, asked of the file, not kept as a running
        # count: after a write that failed partway, a count would be short by
        # the bytes that did land, and every later frame would be indexed at
        # the wrong place. This way they are orphaned, never pointed at.
        off = os.fstat(vf.fileno()).st_size
        _write_all(vf, fr.jpeg)             # raises: no index line written
        self.nbytes += len(fr.jpeg)
        rec: dict[str, Any] = {"tm": fr.tm, "tw": round(fr.tw, 6), "off": off,
                               "len": len(fr.jpeg), "crc": zlib.crc32(fr.jpeg),
                               "pre": [enc_bytes(p) for p in fr.pre]}
        if fr.up is not None:
            rec["up"] = {"tm": fr.up[0], "tw": round(fr.up[1], 6)}
        # After the bytes, so an index line never points at data that is not
        # there yet.
        self._put(fr.cam + INDEX_EXT, rec)

    def flush(self, fsync: bool = False) -> None:
        # Video before index, for the same reason as above.
        for f in [*self._video.values(), *self._text.values()]:
            f.flush()
            if fsync:
                os.fsync(f.fileno())

    def close(self) -> None:
        for f in [*self._video.values(), *self._text.values()]:
            try:
                f.flush()
                os.fsync(f.fileno())
                f.close()
            except Exception:                   # noqa: BLE001
                pass                            # a dead volume; nothing to save
        self._video.clear()
        self._text.clear()


def write_meta(path: Path, meta: dict) -> None:
    """Atomically, so a crash leaves the previous meta.json, never half of one."""
    tmp = path / (META + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path / META)


def read_meta(path: Path) -> dict:
    try:
        obj = json.loads((Path(path) / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


# ---- reading ------------------------------------------------------------ #

def iter_jsonl(path: Path) -> Iterator[dict]:
    """Lines with an integer `tm`. A torn or junk line is skipped, not fatal."""
    try:
        f = open(path, "rb")  # noqa: SIM115
    except FileNotFoundError:
        return
    with f:
        for raw in f:
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("tm"), int):
                yield obj


@dataclass(slots=True)
class Event:
    """One recorded message.

    kind    "mqtt", "apriltag" or "frame"
    source  the MQTT topic, the detector topic, or the camera name
    rec     the stored line as written
    """

    tm: int
    kind: str
    source: str
    rec: dict

    @property
    def payload(self) -> bytes:
        """MQTT / detector payload. (Frames: see Session.jpeg.)"""
        return dec_bytes(self.rec.get("p"))

    @property
    def pre(self) -> list[bytes]:
        """A frame's parts before the JPEG, as the camera sent them."""
        return [dec_bytes(p) for p in self.rec.get("pre") or []]

    @property
    def tm_arrival(self) -> int:
        """Earliest arrival stamp on the Jetson: the tee's for a teed frame,
        else the recorder's own."""
        up = self.rec.get("up")
        if isinstance(up, dict) and isinstance(up.get("tm"), int):
            return up["tm"]
        return self.tm


class Session:
    """Read access to one recorded session."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_dir():
            raise FileNotFoundError(f"no such session directory: {path}")
        self.meta = read_meta(self.path)
        self._video: dict[str, BinaryIO] = {}

    @property
    def name(self) -> str:
        return self.path.name

    def cameras(self) -> list[str]:
        return sorted(p.name[:-len(INDEX_EXT)]
                      for p in self.path.glob("*" + INDEX_EXT))

    def mqtt(self) -> Iterator[Event]:
        for rec in iter_jsonl(self.path / MQTT):
            yield Event(rec["tm"], "mqtt", str(rec.get("topic", "")), rec)

    def apriltag(self) -> Iterator[Event]:
        for rec in iter_jsonl(self.path / APRILTAG):
            yield Event(rec["tm"], "apriltag", str(rec.get("topic", "")), rec)

    def frames(self, cam: str) -> Iterator[Event]:
        """A camera's frames whose bytes are in the .mjpeg. An entry pointing
        past its end -- a cut-off tail, or a damaged line -- is skipped, not
        the end of reading."""
        try:
            size = (self.path / (cam + VIDEO_EXT)).stat().st_size
        except FileNotFoundError:
            return
        for rec in iter_jsonl(self.path / (cam + INDEX_EXT)):
            off, n = rec.get("off"), rec.get("len")
            if not isinstance(off, int) or not isinstance(n, int) or off < 0:
                continue
            if off + n > size:
                continue
            yield Event(rec["tm"], "frame", cam, rec)

    def events(self, mqtt: bool = True, apriltag: bool = True,
               video: bool = True,
               cameras: Optional[list[str]] = None) -> Iterator[Event]:
        """Everything, merged into one stream in arrival (`tm`) order."""
        its: list[Iterator[Event]] = []
        if mqtt:
            its.append(self.mqtt())
        if apriltag:
            its.append(self.apriltag())
        if video:
            for cam in cameras if cameras is not None else self.cameras():
                its.append(self.frames(cam))
        return heapq.merge(*its, key=lambda e: e.tm)

    def jpeg(self, ev: Event, verify: bool = True) -> bytes:
        """A frame's JPEG. Raises CorruptFrameError if its bytes are short or
        fail the CRC-32 recorded with them (sessions recorded before the CRC
        was added are not checked)."""
        f = self._video.get(ev.source)
        if f is None:
            f = open(self.path / (ev.source + VIDEO_EXT), "rb")  # noqa: SIM115
            self._video[ev.source] = f
        f.seek(ev.rec["off"])
        data = f.read(ev.rec["len"])
        if len(data) != ev.rec["len"]:
            raise CorruptFrameError(f"{ev.source} @ {ev.rec['off']}: short read")
        crc = ev.rec.get("crc")
        if verify and isinstance(crc, int) and zlib.crc32(data) != crc:
            raise CorruptFrameError(f"{ev.source} @ {ev.rec['off']}: CRC mismatch")
        return data

    def close(self) -> None:
        for f in self._video.values():
            f.close()
        self._video.clear()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
