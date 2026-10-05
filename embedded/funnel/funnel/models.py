#!/usr/bin/env python3
"""
The data shapes that cross module boundaries.

Two tiers on purpose:

  * Internal hot-path types (StreamKey, Sample, Snapshot) are slotted
    dataclasses -- cheap to allocate at sensor rates and, being frozen where it
    matters, safe to hand to readers without copying.

  * Outward-facing response models are Pydantic, so the OpenAPI document at
    /docs is the contract the mobile client is written against.

Raw per-stream blocks are deliberately `dict[str, Any]`: a sensor's payload is
passed through untouched, so adding a measurement to firmware needs no change
here. The envelope around them -- tick, timing, staleness, counters -- is pinned,
because that is what clients depend on.

Derived blocks are the opposite: SeatPositionResponse pins its shape, because it
is a contract the mobile client is written against and the transformation behind
it (compute/seat_position.py) will be filled in without changing its fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NamedTuple, Optional

from pydantic import BaseModel, Field

# ---- internal ---------------------------------------------------------- #

class StreamKey(NamedTuple):
    """Identifies one stream of samples.

    scope is "rower" or "boat". ident is the 1-based seat number for rower
    streams and None for boat-level ones.
    """

    scope: str
    ident: Optional[int]
    stream: str

    def as_path(self) -> str:
        if self.ident is None:
            return f"{self.scope}/{self.stream}"
        return f"{self.scope}/{self.ident}/{self.stream}"


@dataclass(slots=True)
class Sample:
    """One reading from one stream.

    t_recv  monotonic clock at the instant the funnel received it. Ages and
            rates are always derived from this, never from a sender's clock.
    t_src   the sample's own timestamp mapped onto the host wall clock, or None
            if the sender gave us nothing to map. See store.ClockEstimator for
            why this is an estimate rather than a reading.
    seq     the sender's gapless counter, for drop detection. None if absent.
    values  the decoded payload minus the envelope fields (seq/t_ms/t/dev).
    """

    t_recv: float
    t_src: Optional[float]
    seq: Optional[int]
    values: dict[str, Any]
    dev: Optional[str] = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What one compute tick produced. Published by reference swap, never
    mutated, so a reader always sees a complete and self-consistent view."""

    tick: int
    t: float
    """Wall clock at tick start -- on the even grid, not at emit time."""
    t_recv: float
    """Monotonic clock at tick start, for computing ages at request time."""
    jitter_ms: float
    compute_ms: float
    rowers: dict[int, dict[str, Any]] = field(default_factory=dict)
    boat: dict[str, Any] = field(default_factory=dict)
    streams: dict[str, dict[str, Any]] = field(default_factory=dict)
    presence: dict[int, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def empty() -> Snapshot:
        """The snapshot served before the first tick has run."""
        return Snapshot(tick=0, t=0.0, t_recv=0.0, jitter_ms=0.0, compute_ms=0.0)


# ---- responses --------------------------------------------------------- #

class StreamStats(BaseModel):
    """Per-stream health. Mirrors what /streams reports for every stream."""

    path: str = Field(description="rower/3/seat or boat/imu")
    scope: str
    ident: Optional[int] = None
    stream: str
    age_ms: Optional[float] = Field(
        None, description="since the last sample arrived; null if none ever did"
    )
    rate_hz: Optional[float] = Field(
        None, description="measured over the buffered window, not configured"
    )
    stale: bool = Field(description="age_ms exceeds FUNNEL_MAX_AGE_MS")
    n_recv: int
    n_bad: int = Field(description="payloads that failed to decode")
    n_gap: int = Field(description="samples missing per the sender's seq counter")
    buffered: int = Field(description="samples currently held")
    last_seq: Optional[int] = None
    dev: Optional[str] = None


class SeatPositionResponse(BaseModel):
    """Derived seat position -- the output of compute/seat_position.py.

    Unlike a raw stream block, this shape is pinned: it is a contract the
    mobile client is written against, and implementing the transformation
    stages changes the numbers without changing the fields.
    """

    position: Optional[float] = Field(
        None, description="0.0 at the catch, 1.0 at the finish; null while the "
                          "normalize stage is unimplemented or the seat is "
                          "uncalibrated")
    travel_mm: Optional[float] = Field(
        None, description="millimetres along the slide from the catch. While "
                          "`calibrated` is false this is the sensor's own "
                          "distance reading passed through unchanged -- it is "
                          "not referenced to the catch and is not comparable "
                          "between seats")
    velocity_mms: Optional[float] = Field(
        None, description="along-slide velocity in mm/s, derived from the "
                          "filtered series rather than the sensor's own figure")
    t: Optional[float] = Field(
        None, description="source timestamp of the newest input sample")
    age_ms: Optional[float] = None
    stale: bool
    n_in: int = Field(description="input samples the pipeline saw this tick")
    calibrated: bool = Field(
        description="whether this seat has a usable calibration reference")
    ready: bool = Field(
        description="false while any pipeline stage is still an identity no-op")
    pending: list[str] = Field(
        description="pipeline stages not yet implemented; empty when ready")


class RowerSummary(BaseModel):
    seat: int
    streams: list[str]
    age_ms: Optional[float] = None
    stale: bool
    up: Optional[bool] = Field(
        None, description="from the sensor's retained status topic; null if never seen"
    )


class TickStats(BaseModel):
    tick: int
    hz: float
    jitter_ms: float = Field(description="last tick's lateness against its deadline")
    jitter_ms_max: float
    n_late: int = Field(description="grid points skipped after an overrun")
    n_errors: int = Field(description="compute calls that raised")
    compute_ms: float


class HealthResponse(BaseModel):
    status: str = Field(description='"ok", "stale" or "no-data"')
    uptime_s: float
    tick: TickStats
    sources: dict[str, Any]
    streams: list[StreamStats]
    max_age_ms: float
    recording: Optional[str] = Field(
        None, description="session directory, or null when recording is disabled"
    )
    pipelines: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="derived-value pipelines and each stage's status "
                    '("identity" means not yet implemented)',
    )


class SnapshotResponse(BaseModel):
    """The mobile client's endpoint: everything the last tick computed, in one
    response. Per-stream `stale` flags appear inside the blocks so a single dead
    sensor greys out one tile instead of failing the whole poll."""

    tick: int
    t: float
    age_ms: float = Field(description="since the tick that produced this")
    rowers: dict[int, dict[str, Any]]
    boat: dict[str, Any]
    streams: dict[str, dict[str, Any]]
    presence: dict[int, dict[str, Any]]


class RawResponse(BaseModel):
    path: str
    window_s: float
    n: int
    samples: list[dict[str, Any]]
