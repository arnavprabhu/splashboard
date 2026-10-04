import json
from pathlib import Path

import pytest
import tomlkit

from splash_gui.integrations.snapshots import restore, snapshot


def test_restore_original_bytes(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = b'{"personal": true}\n'
    path.write_bytes(original)
    new = b'{"personal": true, "deploymentMode": "3p"}'
    record = snapshot(path, new, ["deploymentMode"])
    path.write_bytes(new)
    restore(path, record)
    assert path.read_bytes() == original


def test_restore_preserves_new_unrelated_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('# personal comment\nmodel = "native"\n[features]\nfoo = true\n')
    new = b'model = "splash"\nopenai_base_url = "http://localhost:8000"\n'
    record = snapshot(path, new, ["model", "openai_base_url"])
    path.write_text(
        'model = "splash"\nopenai_base_url = "http://localhost:8000"\n'
        "[features]\nfoo = false\nbar = true\n"
    )
    restore(path, record)
    restored = tomlkit.parse(path.read_text())
    assert restored["model"] == "native"
    assert "openai_base_url" not in restored
    assert restored["features"] == {"foo": False, "bar": True}


def test_changed_auth_sentinel_belongs_to_user(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    sentinel = b'{"OPENAI_API_KEY":"splash-local-codex","auth_mode":"apikey"}'
    record = snapshot(path, sentinel)
    path.write_text(json.dumps({"tokens": {"access_token": "new-user-login"}}))
    restore(path, record)
    assert json.loads(path.read_text())["tokens"]["access_token"] == "new-user-login"
    path.write_bytes(sentinel)
    restore(path, record)
    assert not path.exists()


def test_unrelated_file_mutation_is_not_silently_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    record = snapshot(path, b"{}")
    path.write_bytes(b'{"user":true}')
    with pytest.raises(ValueError, match="changed while connected"):
        restore(path, record)
    assert path.read_bytes() == b'{"user":true}'
