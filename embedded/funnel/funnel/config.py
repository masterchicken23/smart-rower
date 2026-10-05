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

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def period_s(self) -> float:
        return 1.0 / self.compute_hz if self.compute_hz > 0 else 0.1


def load() -> Settings:
    return Settings()
