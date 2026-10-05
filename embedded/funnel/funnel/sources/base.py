#!/usr/bin/env python3
"""
What an ingest source has to provide.

Sources differ in transport and in nothing else: each one decodes whatever
arrives into Samples and pushes them into the StreamStore. Adding the ZMQ feed
from the computer-vision process later means writing one more class with this
shape, not touching the buffers, the compute loop or the API.

A source's `run` is a long-lived task. It owns its own reconnect behaviour --
a sensor rebooting or a broker restarting is routine on a boat, not a reason to
take the service down -- and it exits on cancellation.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from ..models import Sample, StreamKey
from ..recorder import Recorder
from ..store import StreamStore


class Source(Protocol):
    name: str

    async def run(self) -> None:
        """Ingest until cancelled."""
        ...

    def stats(self) -> dict[str, Any]:
        """Whatever /health should show about this source."""
        ...


class BaseSource:
    """Shared plumbing: push a sample, and offer it to the recorder if one
    exists. The `is not None` check is the entire cost of recording when
    recording is switched off."""

    name = "source"

    def __init__(self, store: StreamStore,
                 recorder: Optional[Recorder] = None) -> None:
        self.store = store
        self.recorder = recorder
        self.n_msgs = 0
        self.n_bad = 0
        self.n_unknown = 0

    def emit(self, key: StreamKey, sample: Sample) -> None:
        self.store.push(key, sample)
        self.n_msgs += 1
        if self.recorder is not None:
            self.recorder.offer(key, sample)

    def stats(self) -> dict[str, Any]:
        return {
            "messages": self.n_msgs,
            "bad": self.n_bad,
            "unknown_topic": self.n_unknown,
        }
