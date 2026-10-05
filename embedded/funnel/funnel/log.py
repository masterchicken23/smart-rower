#!/usr/bin/env python3
"""
Logging, kept to the convention the rest of this repo uses: a one-line helper
that writes to STDERR with a bracketed subsystem prefix.

Everything diagnostic goes to stderr so stdout stays clean -- see the same rule
in experiments/network-video-yolo/pose_api.py. uvicorn's own loggers are routed
through here by setup() so there is one output format, not two.
"""

from __future__ import annotations

import logging
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


class _StderrHandler(logging.Handler):
    """Funnel stdlib logging (uvicorn, aiomqtt) through log()."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            log(f"[{record.name.split('.')[0]}] {record.getMessage()}")
        except Exception:  # noqa: BLE001
            pass            # logging must never take the service down


def setup(level: str) -> None:
    """Set our level and redirect stdlib logging to stderr in our format."""
    set_level(level)
    root = logging.getLogger()
    root.handlers = [_StderrHandler()]
    root.setLevel(_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
