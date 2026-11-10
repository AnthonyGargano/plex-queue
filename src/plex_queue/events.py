"""Structured application events for future Kafka → Grafana / autotick pipelines.

Human logs stay on normal module loggers. Machine events go to
``plex_queue.events`` as one JSON object per line with a stable ``event`` name
and flat fields. A later Kafka handler can subscribe to that logger name
without rewriting call sites.

Do not put secrets, tokens, or full setup_secret values in fields.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("plex_queue.events")

# Reserved keys that callers must not override.
_RESERVED = frozenset({"event", "ts", "service"})


def emit(event: str, **fields: Any) -> None:
    """Emit one structured event (JSON line on the events logger)."""
    name = str(event or "").strip()
    if not name:
        return
    payload: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "service": "plex-queue",
        "event": name,
    }
    for key, value in fields.items():
        if key in _RESERVED or value is None:
            continue
        payload[key] = _plain(value)
    log.info("%s", json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def timed() -> float:
    """Monotonic start mark for ``duration_ms`` fields."""
    return time.monotonic()


def duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _plain(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value[:50]]
    return str(value)
