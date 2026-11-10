from __future__ import annotations

import json
import logging

from plex_queue.events import emit


def test_emit_json_line(caplog):
    caplog.set_level(logging.INFO, logger="plex_queue.events")
    emit("tidy.apply", status="ok", action_id="abc", user_id=1)
    records = [r for r in caplog.records if r.name == "plex_queue.events"]
    assert records
    payload = json.loads(records[-1].getMessage())
    assert payload["event"] == "tidy.apply"
    assert payload["service"] == "plex-queue"
    assert payload["status"] == "ok"
    assert payload["action_id"] == "abc"
    assert "ts" in payload


def test_emit_strips_none_and_reserved():
    import logging as logging_mod

    records = []

    class Capture(logging_mod.Handler):
        def emit(self, record):
            records.append(record)

    handler = Capture()
    log = logging_mod.getLogger("plex_queue.events")
    previous = log.level
    log.setLevel(logging_mod.INFO)
    log.addHandler(handler)
    try:
        emit("test.event", **{"ts": "nope", "service": "other", "ok": 1, "skip": None})
    finally:
        log.removeHandler(handler)
        log.setLevel(previous)
    assert records
    payload = json.loads(records[-1].getMessage())
    assert payload["event"] == "test.event"
    assert payload["service"] == "plex-queue"
    assert payload["ok"] == 1
    assert "skip" not in payload
