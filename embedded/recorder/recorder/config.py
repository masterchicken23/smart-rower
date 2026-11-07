#!/usr/bin/env python3
"""
Configuration. Every setting is an environment variable prefixed REC_, for the
same reason the funnel uses FUNNEL_ and the detectors APRIL_: compose is the
configuration surface for a container. Names carry their units.

The hand-run tools (`python -m recorder.report`, `python -m recorder.play`)
take argparse flags instead, like every other CLI in the repo.

`python -c "from recorder.config import load; print(load())"` prints the
effective settings.
"""

from __future__ import annotations

import re

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CAMERA_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
"""Camera names become file names (`cam1.mjpeg`), so they are kept to a set of
characters that is safe on every filesystem."""


def parse_cameras(spec: str) -> dict[str, str]:
    """`cam1=tcp://127.0.0.1:5571,cam2=...` -> {"cam1": "tcp://...", ...}.

    Raises ValueError, so a bad map stops startup instead of recording a
    camera under a name nobody expects.
    """
    out: dict[str, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, endpoint = item.partition("=")
        name, endpoint = name.strip(), endpoint.strip()
        if not sep or not name or not endpoint:
            raise ValueError(f"expected <name>=<endpoint>, got {item!r}")
        if not CAMERA_NAME.match(name):
            raise ValueError(f"camera name {name!r} may only use A-Z a-z 0-9 _ -")
        if name in out:
            raise ValueError(f"camera {name!r} is listed twice")
        out[name] = endpoint
    return out


def split_list(spec: str) -> list[str]:
    return [p.strip() for p in spec.split(",") if p.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REC_", env_file=".env", extra="ignore"
    )

    # -- storage ------------------------------------------------------------ #
    dir: str = "/data"
    """Where sessions are created, one directory per session named after its
    UTC start time. Put it on the NVMe: four cameras write ~2 MB/s."""
    min_free_gb: float = 2.0
    """A session will not start, and a running one stops, when the volume has
    less free space than this. A full root filesystem takes the whole Jetson
    down with it, not just the recording."""

    # -- HTTP control -------------------------------------------------------- #
    http_host: str = "0.0.0.0"
    http_port: int = 8010
    """Not 8000: that is the funnel's. Every route is also under /recorder, so
    the two APIs cannot collide even behind one proxy."""
    cors: str = "*"
    access_log: bool = False

    # -- MQTT: the sensors -------------------------------------------------- #
    mqtt_host: str = "127.0.0.1"
    """The funnel's mosquitto publishes 1883 on the host, and the recorder
    runs with host networking, so localhost is the broker."""
    mqtt_port: int = 1883
    mqtt_topics: str = "#"
    """Everything on the boat's broker by default, including topics the funnel
    does not ingest: a raw recording should not decide what matters."""
    mqtt_client_id: str = ""
    """Empty: `recorder-<pid>`, so two recorders on one broker (the replay
    check) do not keep disconnecting each other."""
    mqtt_keepalive_s: int = 15

    # -- ZMQ: the cameras and the detectors --------------------------------- #
    cameras: str = ("cam1=tcp://127.0.0.1:5571,cam2=tcp://127.0.0.1:5572,"
                    "cam3=tcp://127.0.0.1:5573,cam4=tcp://127.0.0.1:5574")
    """`<name>=<endpoint>` per camera. The defaults are the april-detect raw
    frame tees (APRIL_TEE_ADDRESS). A camera's own address works too
    (`cam1=tcp://cam1.local:5555`), at the cost of a second copy of its stream
    over Wi-Fi. Empty disables video."""
    apriltag_connect: str = ("tcp://127.0.0.1:5561,tcp://127.0.0.1:5562,"
                             "tcp://127.0.0.1:5563,tcp://127.0.0.1:5564")
    """Detector output addresses, as the funnel's FUNNEL_APRILTAG_CONNECT.
    Empty disables recording detections."""
    zmq_rcvhwm: int = 100
    """Large on purpose. The detectors keep only the newest frame; a recorder
    wants every one, and its receive threads do nothing that could fall
    behind."""

    # -- writing ------------------------------------------------------------ #
    queue_mb: float = 256.0
    """Bound on data waiting for the disk. About two minutes of four default
    cameras; past it the oldest waiting item is dropped and counted."""
    flush_s: float = 1.0
    fsync_s: float = 5.0
    """How much a pulled battery can cost. 0 disables fsync."""
    disk_check_s: float = 10.0

    # -- misc --------------------------------------------------------------- #
    autostart: bool = False
    """Start a session as soon as the container is up, for headless use."""
    stats_interval_s: float = 5.0
    log_level: str = "info"

    @field_validator("cameras")
    @classmethod
    def _check_cameras(cls, v: str) -> str:
        parse_cameras(v)
        return v

    @property
    def camera_map(self) -> dict[str, str]:
        return parse_cameras(self.cameras)

    @property
    def apriltag_endpoints(self) -> list[str]:
        return split_list(self.apriltag_connect)

    @property
    def topics(self) -> list[str]:
        return split_list(self.mqtt_topics)


def load() -> Settings:
    return Settings()
