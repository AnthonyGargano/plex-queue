from pathlib import Path

from plex_queue.access import Access
from plex_queue.bot import Limiter
from plex_queue.config import Config
from plex_queue.sshutil import ensure_known_host, host_pinned, ssh_options


def test_claim_is_first_writer_wins(tmp_path):
    config = _config(tmp_path, owner_id=0)
    access = Access(config)
    assert "You are the owner" in access.claim(11)
    assert access.claim(22) == "This bot already has an owner."
    assert access.owner_id == 11


def test_corrupt_access_json_is_quarantined(tmp_path):
    config = _config(tmp_path, owner_id=0)
    path = config.data_dir / "access.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    access = Access(config)
    assert access.owner_id == 0
    assert not path.exists()
    assert path.with_suffix(".json.bad").exists()


def test_persisted_owner_wins_over_yaml_owner(tmp_path):
    config = _config(tmp_path, owner_id=7)
    path = config.data_dir / "access.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"owner_id": 11, "allowed_user_ids": [22]}', encoding="utf-8")
    access = Access(config)
    assert access.owner_id == 11
    assert access.allowed == {22}


def test_corrupt_ledger_json_is_quarantined(tmp_path):
    from plex_queue.ledger import Ledger

    path = tmp_path / "imports.json"
    path.write_text("{broken", encoding="utf-8")
    ledger = Ledger(path)
    assert ledger.states() == {}
    assert not path.exists()
    assert path.with_suffix(".json.bad").exists()


def test_blank_ssh_key_path_is_rejected(tmp_path):
    import pytest
    from plex_queue import config as config_mod

    path = tmp_path / "config.yaml"
    path.write_text(
        "telegram:\n  token: x\n  setup_secret: secret\ndeluge:\n  ssh:\n    key_path: ''\n"
        "    host: 1.2.3.4\n    user: ttt\nlibrary:\n  roots: []\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="key_path"):
        config_mod.load_config(path)


def test_claim_attempts_have_their_own_rate_limit():
    limiter = Limiter()
    assert limiter.allow(9, 2) is True
    assert limiter.allow(9, 2) is True
    assert limiter.allow(9, 2) is False


def test_limiter_cost_blocks_collection_sweeps_sooner():
    limiter = Limiter()
    assert limiter.allow(3, 10, cost=10) is True
    assert limiter.allow(3, 10, cost=10) is False
    assert limiter.allow(3, 10, cost=1) is False


def test_ssh_options_pin_to_a_dedicated_known_hosts(tmp_path):
    known = tmp_path / "ssh_known_hosts"
    options = ssh_options(known)
    assert "StrictHostKeyChecking=yes" in options
    assert f"UserKnownHostsFile={known}" in " ".join(options)
    assert "accept-new" not in " ".join(options)


def test_ensure_known_host_writes_and_detects_the_pin(tmp_path):
    known = tmp_path / "ssh_known_hosts"

    def run(command, capture_output, text, check):
        class Completed:
            def __init__(self, stdout="", code=0):
                self.stdout = stdout
                self.stderr = ""
                self.returncode = code

        joined = " ".join(str(part) for part in command)
        if "ssh-keyscan" in joined:
            return Completed(
                "172.30.161.89 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTestsOnly0000000000000000\n"
                "evil.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIShouldNotBePinned000000000000000000\n"
            )
        if "ssh-keygen" in joined:
            return Completed("256 SHA256:fake 172.30.161.89 (ED25519)\n")
        return Completed(code=1)

    assert ensure_known_host("172.30.161.89", known, keyscan="ssh-keyscan", run=run)
    assert host_pinned("172.30.161.89", known)
    assert "evil.example" not in known.read_text(encoding="utf-8")
    assert ensure_known_host("172.30.161.89", known, keyscan="ssh-keyscan", run=run)


def test_ensure_known_host_falls_back_to_a_one_time_ssh_pin(tmp_path):
    known = tmp_path / "ssh_known_hosts"
    key = tmp_path / "key"
    key.write_text("x", encoding="utf-8")
    calls = []

    def run(command, capture_output, text, check):
        calls.append(command)

        class Completed:
            def __init__(self, code, stdout=""):
                self.stdout = stdout
                self.stderr = ""
                self.returncode = code

        joined = " ".join(str(part) for part in command)
        if "ssh-keyscan" in joined:
            return Completed(1)
        if "ssh-keygen" in joined:
            return Completed(0, "256 SHA256:fake 172.30.161.89 (ED25519)\n")
        # Simulate ssh accept-new writing the known_hosts entry.
        known.write_text(
            "172.30.161.89 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTestsOnly0000000000000000\n",
            encoding="utf-8",
        )
        return Completed(0)

    assert ensure_known_host(
        "172.30.161.89",
        known,
        key=key,
        user="ttt",
        keyscan="ssh-keyscan",
        ssh="ssh",
        run=run,
    )
    assert host_pinned("172.30.161.89", known)
    assert any("accept-new" in " ".join(command) for command in calls)


def _config(tmp_path: Path, owner_id: int = 0) -> Config:
    return Config(
        path=tmp_path / "config.yaml",
        token="x",
        setup_secret="secret",
        owner_id=owner_id,
        allowed_user_ids=(),
        deluge_username="localclient",
        deluge_password="pw",
        deluge_download_location="",
        ssh_host="172.30.161.89",
        ssh_user="ttt",
        ssh_key=tmp_path / "key",
        ssh_local_port=15884,
        ssh_remote_port=58846,
        ssh_known_hosts=tmp_path / "ssh_known_hosts",
        min_height=1080,
        plex_db=None,
        roots=(),
        search_endpoint="https://apibay.org/q.php",
        vm_name="UbuntuClean",
        data_dir=tmp_path / "data",
        movie_dirs=(),
        show_dirs=(),
        trash_dir=None,
        import_margin=0,
        import_every=600,
        tidy_auto=False,
        tidy_buckets={},
    )
