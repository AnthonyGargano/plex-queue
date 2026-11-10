from __future__ import annotations

import json

from plex_queue.signed_state import read_signed_json, state_mac, write_signed_json
from plex_queue.tidy_ledger import TidyLedger


SECRET = "sixteen-char-secret"


def test_write_and_read_signed(tmp_path):
    path = tmp_path / "tidy.json"
    write_signed_json(path, {"actions": {"a": {"id": "a"}}}, SECRET)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "mac" in raw
    body = {k: v for k, v in raw.items() if k != "mac"}
    assert raw["mac"] == state_mac(SECRET, body)
    loaded = read_signed_json(path, SECRET, label="tidy ledger")
    assert loaded["actions"]["a"]["id"] == "a"


def test_invalid_mac_quarantines(tmp_path):
    path = tmp_path / "tidy.json"
    write_signed_json(path, {"actions": {}}, SECRET)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["mac"] = "deadbeef"
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = read_signed_json(path, SECRET, label="tidy ledger")
    assert loaded == {}
    assert path.with_suffix(".json.bad").exists() or list(tmp_path.glob("*.bad"))


def test_unsigned_quarantines_when_secret_ready(tmp_path):
    """Tampered unsigned ledgers must not be auto-trusted / re-signed."""
    path = tmp_path / "tidy.json"
    planted = {
        "actions": {
            "x": {
                "id": "x",
                "status": "pending",
                "kind": "trash",
                "source": r"D:\Movies\Innocent.mkv",
            }
        }
    }
    path.write_text(json.dumps(planted), encoding="utf-8")
    loaded = read_signed_json(path, SECRET, label="tidy ledger")
    assert loaded == {}
    bad = list(tmp_path.glob("*.bad"))
    assert bad
    assert not path.exists()
    # Quarantined body must not become a valid signed ledger on reload.
    ledger = TidyLedger(path, secret=SECRET)
    assert ledger.get("x") is None


def test_setup_secret_still_loads_unsigned(tmp_path):
    path = tmp_path / "tidy.json"
    path.write_text(json.dumps({"actions": {"x": {"id": "x"}}}), encoding="utf-8")
    loaded = read_signed_json(path, "", label="tidy ledger")
    assert loaded["actions"]["x"]["id"] == "x"
