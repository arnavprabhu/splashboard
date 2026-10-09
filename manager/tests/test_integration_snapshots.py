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


def test_a_file_never_written_is_left_exactly_alone(tmp_path: Path) -> None:
    """A crash between state.json and this file's write (or a failure on an
    earlier file) leaves it untouched; restore must not re-encode it."""
    path = tmp_path / "claude_desktop_config.json"
    original = b'{\n\t"mcpServers": {},  "x": 1\n}'  # not json.dumps(indent=2) formatting
    path.write_bytes(original)
    record = snapshot(path, b'{"deploymentMode": "3p"}\n', ["deploymentMode"])
    record["absent_defaults"] = {"deploymentMode": "1p"}
    restore(path, record)
    assert path.read_bytes() == original


def test_a_symlinked_config_is_written_through_and_keeps_its_link(tmp_path: Path) -> None:
    dotfiles = tmp_path / "dotfiles" / "config.toml"
    dotfiles.parent.mkdir()
    original = b'model = "native"\n'
    dotfiles.write_bytes(original)
    dotfiles.chmod(0o644)
    link = tmp_path / ".codex" / "config.toml"
    link.parent.mkdir()
    link.symlink_to("../dotfiles/config.toml")
    new = b'model = "splash"\n'
    record = snapshot(link, new, ["model"])
    assert record["target"] == str(dotfiles.resolve())
    # What connect does with the record.
    from splash_gui.paths import write_atomic

    write_atomic(Path(record["target"]), new, record["mode"])
    assert link.is_symlink() and link.read_bytes() == new
    restore(link, record)
    assert link.is_symlink(), "the user's dotfile link survives"
    assert str(link.readlink()) == "../dotfiles/config.toml"
    assert dotfiles.read_bytes() == original
    assert dotfiles.stat().st_mode & 0o777 == 0o644


def test_restore_puts_back_the_original_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b"{}\n")
    path.chmod(0o644)
    record = snapshot(path, b'{"a": 1}\n', ["a"])
    path.write_bytes(b'{"a": 1}\n')
    path.chmod(0o600)
    restore(path, record)
    assert path.read_bytes() == b"{}\n"
    assert path.stat().st_mode & 0o777 == 0o644
