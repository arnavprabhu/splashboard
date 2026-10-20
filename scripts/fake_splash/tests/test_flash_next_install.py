"""The install side of Qwen3.8-Flash-Next as Splash 1.4.0 has it: a third family that
loads from a GGUF only, split GGUFs installed from a folder named after their variant,
`select_gguf` answering with a list of files, and a family without a released draft."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_installer import install, repo_dir

from harness import PKG

sys.path.insert(0, str(PKG))

from install import families, hub, models, signatures, upstream

FLASH = "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF"
PARTS = [f"Q2_0/Qwen3.8-Flash-Next-GSQ-RCO-Q2_0-0000{k}-of-00002.gguf" for k in (1, 2)]


def test_a_split_variant_installs_its_parts_side_by_side(tmp_path):
    code, out, err = install(tmp_path, "--model", f"{FLASH}:Q2_0", "prepare")
    assert code == 0, err
    assert f"Selected 2 split files {PARTS[0]} ... from {FLASH}." in out
    assert (
        f"Installing {FLASH}:Q2_0 as Qwen3.8-Flash-Next (gguf); draft incoai/Qwen3.8-Flash-Next-DFlash2; "
        "vision enabled." in out
    )
    folder = repo_dir(tmp_path, FLASH)
    snapshot = folder / "snapshots" / (folder / "refs" / "main").read_text()
    assert all((snapshot / name).is_symlink() for name in PARTS)
    link = tmp_path / "links" / "ISTA-DASLab" / "Qwen3.8-Flash-Next-GSQ-RCO-GGUF:Q2_0"
    record = json.loads((link / "model.json").read_text())
    assert record["family"] == "Qwen3.8-Flash-Next" and record["target_format"] == "gguf"
    assert {name for name in record["files"] if name.startswith("target/")} == {
        "target/" + Path(name).name for name in PARTS
    }
    assert record["sources"]["draft"]["repo"] == "incoai/Qwen3.8-Flash-Next-DFlash2"


def test_flash_next_loads_from_a_gguf_only(tmp_path):
    code, _, err = install(tmp_path, "--model", "rapid-mlx/Qwen3.8-Flash-Next-4bit", "prepare")
    assert code == 1
    assert "Qwen3.8-Flash-Next loads from a GGUF only" in err
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"text_config": signatures.text_config(families.named("Qwen3.8-Flash-Next"))}))
    assert upstream.check_model("gguf", "none", config).name == "Qwen3.8-Flash-Next"
    with pytest.raises(models.ModelError, match=r"^Qwen3\.8-Flash-Next loads from a GGUF only$"):
        upstream.check_model("mlx-affine", "none", config)


def test_select_gguf_answers_with_a_list_and_falls_back_to_a_variant_folder():
    files = ["mmproj-BF16.gguf", *PARTS, "IQ2_XS/M-IQ2_XS-00001-of-00002.gguf"]
    assert upstream.select_gguf(files, "Q2_0") == (PARTS, False)
    assert upstream.select_gguf(["M-Q4_K_M.gguf", "M-Q8_0.gguf"], "Q8_0") == (["M-Q8_0.gguf"], False)
    with pytest.raises(models.ModelError, match=r"the split GGUF in IQ2_XS/ is incomplete"):
        upstream.select_gguf(files, "IQ2_XS")
    with pytest.raises(
        models.ModelError, match=r"select a GGUF with OWNER/REPO:VARIANT \(files in the repository root: none"
    ):
        upstream.select_gguf(files, None)
    # A root file answering :VARIANT comes before the folder, an MTP head too.
    head = "mtp-Qwen3.8-Flash-Next-Q8_0.gguf"
    q8 = [f"Q8_0/Qwen3.8-Flash-Next-Q8_0-0000{k}-of-00002.gguf" for k in (1, 2)]
    assert upstream.select_gguf([head, *q8], "Q8_0") == ([head], False)
    assert upstream.select_gguf([head, *q8], None) == ([head], False)


def test_a_family_without_a_released_draft_needs_draft_model(monkeypatch):
    family = families.ModelFamily("Qwen3.8-Flash-Next", None)
    selection = models.Selection.of(Path("/nowhere"), f"{FLASH}:Q2_0")
    with pytest.raises(models.ModelError, match=r"^no DFlash2 draft is released for Qwen3\.8-Flash-Next; name one"):
        models.draft_repo(selection, family)
    named = models.Selection.of(Path("/nowhere"), f"{FLASH}:Q2_0", draft_model="me/Draft")
    assert models.draft_repo(named, family).name == "me/Draft"


def test_the_draft_signature_and_local_files(tmp_path):
    flash = families.named("Qwen3.8-Flash-Next")
    assert signatures.draft_config(flash)["intermediate_size"] == 7680
    part = tmp_path / "part.gguf"
    part.write_bytes(b"GGUF")
    repo = hub.Repository.local_files("local", {"Q2_0/part.gguf": part})
    assert repo.revision is None and repo.files == {"Q2_0/part.gguf"}
    with repo.open("Q2_0/part.gguf") as stream:
        assert stream.read() == b"GGUF"
    assert repo.file("Q2_0/part.gguf") == part
