#!/usr/bin/env python3
"""
Configuration. Every setting is an environment variable prefixed FUNNEL_,
because docker-compose is the configuration surface for this service.

(The rest of the repo configures scripts with argparse. That is the right choice
for a hand-run CLI and the wrong one for a container, where compose already owns
the environment. Flag names keep the repo's habit of carrying their units:
COMPUTE_HZ, MAX_AGE_MS, BUFFER_SECONDS.)
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def parse_camera_seats(spec: str) -> dict[str, int]:
    """`cam1:1,cam2:2` -> {"cam1": 1, "cam2": 2}.

    One camera per seat and one seat per camera: two cameras merged into one
    seat's stream would interleave their `tick` counters and turn every record
    into a counted gap. Raises ValueError, so a bad map stops startup rather
    than filing a camera's tags under the wrong rower.
    """
    out: dict[str, int] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        cam, sep, seat_s = item.rpartition(":")
        cam = cam.strip()
        if not sep or not cam or "/" in cam:
            raise ValueError(f"expected <camera>:<seat>, got {item!r}")
        try:
            seat = int(seat_s)
        except ValueError:
            raise ValueError(f"seat in {item!r} is not an integer") from None
        if seat < 1:
            raise ValueError(f"seat in {item!r} must be >= 1 (bow = 1)")
        if cam in out:
            raise ValueError(f"camera {cam!r} is mapped twice")
        if seat in out.values():
            raise ValueError(f"seat {seat} has more than one camera")
        out[cam] = seat
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FUNNEL_", env_file=".env", extra="ignore"
    )

    # -- ingest: MQTT ------------------------------------------------------- #
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_client_id: str = "funnel"
    mqtt_topics: str = "rower/+/+,boat/+"
    """Comma-separated subscriptions. Kept as a plain string rather than a list
    so a compose `environment:` entry needs no JSON quoting."""
    mqtt_keepalive_s: int = 15
    mqtt_reconnect_s: float = 2.0

    # -- ingest: ZMQ (computer vision results; not wired up yet) ------------ #
    zmq_enabled: bool = False
    zmq_subscribe: str = "tcp://127.0.0.1:5557"

    # -- ingest: ZMQ (AprilTag detections from vision/april-detect) --------- #
    apriltag_enabled: bool = False
    apriltag_connect: str = ("tcp://127.0.0.1:5561,tcp://127.0.0.1:5562,"
                             "tcp://127.0.0.1:5563,tcp://127.0.0.1:5564")
    """Comma-separated detector PUB addresses, one per camera. One SUB socket
    connect()s to all of them. A plain string for the same reason as
    mqtt_topics."""
    apriltag_cameras: str = ""
    """Which rower each camera watches, as `cam1:1,cam2:2,...` (camera id as
    the detector reports it, 1-based seat). Empty by default on purpose: a
    camera with no seat is ignored and listed under /health rather than guessed
    at, for the same reason an unprovisioned seat sensor must not default to
    seat 1."""
    apriltag_rcvhwm: int = 64
    """Receive high-water mark. Small, so a stalled funnel drops records rather
    than serving a backlog of old frames when it catches up."""

    # -- ingest: recorded-session replay (debug) ---------------------------- #
    replay_path: str = ""
    """A session directory under record_dir. Set it and replay takes the place
    of MQTT ingest, so metrics can be developed with no boat and no sensors."""
    replay_speed: float = 1.0
    replay_loop: bool = True

    # -- buffers ------------------------------------------------------------ #
    buffer_seconds: float = 10.0
    """How much history the compute loop may look back over."""
    buffer_max_samples: int = 2048
    """Hard per-stream cap, so an unexpectedly fast publisher cannot grow
    memory without bound regardless of buffer_seconds."""

    # -- compute loop ------------------------------------------------------- #
    compute_hz: float = 10.0

    # -- HTTP --------------------------------------------------------------- #
    http_host: str = "0.0.0.0"
    """0.0.0.0 exposes the API on the LAN, which is what a phone needs.
    There is no authentication: this is a boat-local network."""
    http_port: int = 8000
    cors: str = "*"
    """Access-Control-Allow-Origin value; empty string disables CORS."""
    max_age_ms: float = 1000.0
    """Older than this and a per-resource endpoint answers 503 rather than
    presenting a stale reading as current (0 disables the check)."""
    access_log: bool = False

    # -- raw recording (debug runs only; off in normal operation) ----------- #
    record_enabled: bool = False
    record_dir: str = "/data"
    record_queue: int = 4096
    record_flush_s: float = 1.0

    # -- misc --------------------------------------------------------------- #
    log_level: str = "info"

    @property
    def topics(self) -> list[str]:
        return [t.strip() for t in self.mqtt_topics.split(",") if t.strip()]

    @field_validator("apriltag_cameras")
    @classmethod
    def _check_camera_seats(cls, v: str) -> str:
        parse_camera_seats(v)
        return v

    @property
    def camera_seats(self) -> dict[str, int]:
        return parse_camera_seats(self.apriltag_cameras)

    @property
    def apriltag_endpoints(self) -> list[str]:
        return [e.strip() for e in self.apriltag_connect.split(",") if e.strip()]

    @property
    def period_s(self) -> float:
        return 1.0 / self.compute_hz if self.compute_hz > 0 else 0.1


def load() -> Settings:
    return Settings()
