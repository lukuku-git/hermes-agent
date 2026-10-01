"""Operator interrupt marker contract (operator → running gateway).

The gateway has no external control channel; drain uses a file marker
(gateway/drain_control.py). Stopping a burst of turns needs the same shape but
narrower: interrupt only the running turns of given chats that started after a
given time, and leave every other session — and the gateway — alone.

On 2026-10-01 an alert burst opened ~30 turns in one internal channel. Nothing
outside the gateway could stop just those; restarting would have killed
legitimate work too.

Contract:
  * request → ``{HERMES_HOME}/.interrupt_request.json`` with
    ``{"request_id": str, "chat_ids": [str, ...], "started_after": float|null,
       "reason": str}``.
  * the gateway watcher interrupts matching running turns once, writes
    ``{HERMES_HOME}/.interrupt_result.json`` ``{"request_id", "interrupted": [...]}``
    and removes the request.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from hermes_constants import get_hermes_home

REQUEST_NAME = ".interrupt_request.json"
RESULT_NAME = ".interrupt_result.json"


def _path(name: str) -> Path:
    return Path(get_hermes_home()) / name


def write_request(request_id: str, chat_ids: Iterable[str], started_after: Optional[float],
                  reason: str) -> Path:
    chats = sorted({str(c).strip() for c in chat_ids if str(c).strip()})
    if not chats:
        raise ValueError("at least one chat id is required")
    path = _path(REQUEST_NAME)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"request_id": request_id, "chat_ids": chats,
                               "started_after": started_after, "reason": reason[:200]}),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_request() -> Optional[dict]:
    path = _path(REQUEST_NAME)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("request_id") or not data.get("chat_ids"):
        return None
    return data


def matches(session_key: str, chat_ids: Iterable[str], started_at: Optional[float],
            started_after: Optional[float]) -> bool:
    """A session key carries its chat id as a ':'-separated segment."""
    segments = set(str(session_key).split(":"))
    if not any(chat in segments for chat in chat_ids):
        return False
    if started_after is None:
        return True
    return started_at is not None and started_at >= float(started_after)


def finish(request_id: str, interrupted: list[str]) -> None:
    result = _path(RESULT_NAME)
    tmp = result.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"request_id": request_id, "interrupted": interrupted}),
                   encoding="utf-8")
    os.replace(tmp, result)
    try:
        _path(REQUEST_NAME).unlink()
    except FileNotFoundError:
        pass
