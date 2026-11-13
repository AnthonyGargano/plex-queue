from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from plex_queue import config as config_mod
from plex_queue.events import emit
from plex_queue.otel_setup import setup_otel, shutdown_otel, validate_otel_endpoint


def test_validate_otel_endpoint_loopback_only():
    assert validate_otel_endpoint("http://127.0.0.1:4317").endswith(":4317")
    assert validate_otel_endpoint("http://localhost").endswith(":4317")
    assert validate_otel_endpoint("http://[::1]") == "http://[::1]:4317"
    with pytest.raises(ValueError, match="loopback"):
        validate_otel_endpoint("http://example.com:4317")
    with pytest.raises(ValueError, match="credentials"):
        validate_otel_endpoint("http://user:pass@127.0.0.1:4317")


def test_emit_strips_sensitive_keys(caplog):
    import json
    import logging

    caplog.set_level(logging.INFO, logger="plex_queue.events")
    shutdown_otel()
    emit(
        "test.event",
        status="ok",
        token="SECRET",
        setup_secret="nope",
        auth_token="also-secret",
        action_id="a1",
        counts={"token": "nested-should-drop", "dupes": 2},
    )
    records = [r for r in caplog.records if r.name == "plex_queue.events"]
    payload = json.loads(records[-1].getMessage())
    assert payload["action_id"] == "a1"
    assert "token" not in payload
    assert "setup_secret" not in payload
    assert "auth_token" not in payload
    assert payload["counts"] == {"dupes": 2}


def test_otel_attrs_strips_sensitive_and_nested_dicts():
    from plex_queue.otel_setup import _otel_attrs

    attrs = _otel_attrs(
        "test.event",
        {
            "status": "ok",
            "token": "SECRET",
            "access_token": "x",
            "action_id": "a1",
            "nested": {"token": "hidden"},
        },
    )
    assert attrs["event"] == "test.event"
    assert attrs["action_id"] == "a1"
    assert attrs["status"] == "ok"
    assert "token" not in attrs
    assert "access_token" not in attrs
    assert "nested" not in attrs


def test_setup_otel_disabled_is_noop():
    assert setup_otel(
        enabled=False,
        endpoint="http://127.0.0.1:4317",
        service_name="plex-queue",
    ) is False
    shutdown_otel()


def test_setup_otel_rejects_remote_endpoint():
    assert setup_otel(
        enabled=True,
        endpoint="http://8.8.8.8:4317",
        service_name="plex-queue",
    ) is False


def test_emit_still_json_when_otel_inactive(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="plex_queue.events")
    shutdown_otel()
    emit("tidy.apply", status="ok", action_id="abc")
    records = [r for r in caplog.records if r.name == "plex_queue.events"]
    assert records
    payload = records[-1].getMessage()
    assert '"event":"tidy.apply"' in payload
    assert '"action_id":"abc"' in payload


def test_load_config_otel_defaults(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "telegram": {
                    "token": "PUT_BOT_TOKEN_HERE",
                    "setup_secret": "change-this-secret",
                },
                "deluge": {
                    "password": "x",
                    "ssh": {"host": "1.2.3.4", "user": "ttt", "key_path": str(tmp_path / "k")},
                },
                "library": {"roots": []},
                "search": {"endpoint": "https://example.test/q.php"},
            }
        ),
        encoding="utf-8",
    )
    # Avoid DNS resolve of example.test in this unit test.
    original = config_mod.validate_search_endpoint

    def _no_resolve(endpoint: str, *, resolve: bool = True) -> str:
        return original(endpoint, resolve=False)

    config_mod.validate_search_endpoint = _no_resolve  # type: ignore[assignment]
    try:
        cfg = config_mod.load_config(path)
    finally:
        config_mod.validate_search_endpoint = original  # type: ignore[assignment]
    assert cfg.otel_enabled is False
    assert cfg.otel_endpoint == "http://127.0.0.1:4317"
    assert cfg.otel_service_name == "plex-queue"


def test_load_config_otel_enabled(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "telegram": {
                    "token": "PUT_BOT_TOKEN_HERE",
                    "setup_secret": "change-this-secret",
                },
                "deluge": {
                    "password": "x",
                    "ssh": {"host": "1.2.3.4", "user": "ttt", "key_path": str(tmp_path / "k")},
                },
                "library": {"roots": []},
                "search": {"endpoint": "https://example.test/q.php"},
                "otel": {
                    "enabled": True,
                    "endpoint": "http://127.0.0.1:4317",
                    "service_name": "plex-queue-lab",
                },
            }
        ),
        encoding="utf-8",
    )
    original = config_mod.validate_search_endpoint

    def _no_resolve(endpoint: str, *, resolve: bool = True) -> str:
        return original(endpoint, resolve=False)

    config_mod.validate_search_endpoint = _no_resolve  # type: ignore[assignment]
    try:
        cfg = config_mod.load_config(path)
    finally:
        config_mod.validate_search_endpoint = original  # type: ignore[assignment]
    assert cfg.otel_enabled is True
    assert cfg.otel_service_name == "plex-queue-lab"
