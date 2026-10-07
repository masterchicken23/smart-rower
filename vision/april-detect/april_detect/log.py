#!/usr/bin/env python3
"""
Logging, kept to the convention the rest of this repo uses: a one-line helper
that writes to STDERR with a bracketed subsystem prefix. Same module as
embedded/funnel/funnel/log.py, minus the uvicorn plumbing this service has no
use for.

Everything diagnostic goes to stderr. Detections never go to stdout either --
they go out over ZMQ -- so stdout is simply unused.
"""

from __future__ import annotations

import sys
from typing import Any

LEVELS = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40}

_level = 20


def set_level(name: str) -> None:
    global _level
    _level = LEVELS.get(name.strip().lower(), 20)


def log(*a: Any) -> None:
    """Unconditional line to stderr. The repo's baseline logger."""
    print(*a, file=sys.stderr, flush=True)


def debug(*a: Any) -> None:
    if _level <= 10:
        log(*a)


def info(*a: Any) -> None:
    if _level <= 20:
        log(*a)


def warn(*a: Any) -> None:
    if _level <= 30:
        log(*a)


def error(*a: Any) -> None:
    if _level <= 40:
        log(*a)
