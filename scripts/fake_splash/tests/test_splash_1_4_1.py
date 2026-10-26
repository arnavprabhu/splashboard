"""Splash 1.4.1 in the fake: Flint and Ornith-1.5-122B (the Qwen3.5-122B-A10B
family) as installs, Flint's /status (its sliding-window KV and
`reasoning_switch`), its context bound and its own choice to reason, and the
start failures that now end with the engine's reason."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from test_installer import install
from test_status_1_4_0 import engine, job, run
from test_status_contract import ENGINE, flatten

from harness import PKG

sys.path.insert(0, str(PKG))

from install import families, models, signatures, upstream
from server.fake_shapes import FakeJob

FLINT = "unsloth/flint-GGUF:UD-Q4_K_XL"
ORNITH = "unsloth/Ornith-1.5-122B-GGUF:UD-Q4_K_XL"
ORNITH_MLX = "ornith-ai/Ornith-1.5-122B-MLX-4bit"
DENSE = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M"


def record(tmp_path: Path, model: str) -> dict:
    owner, repo = model.split("/", 1)
    return json.loads((tmp_path / "links" / owner / repo / "model.json").read_text())


def test_the_families_are_splash_1_4_1s():
    assert [f.name for f in families.FAMILIES] == [
        "Qwen3.8-27B",
        "Qwen3.6-35B-A3B",
        "Qwen3.5-122B-A10B",
        "Qwen3.8-Flash-Next",
        "Flint",
    ]
    assert families.named("Flint").vision is False
    assert families.named("Flint").chat_format.reasoning_switch is False
    assert families.named("Qwen3.5-122B-A10B").draft_repo == "incoai/Ornith-1.5-122B-DFlash2"


def test_flint_installs_text_only_with_its_draft(tmp_path):
    code, out, err = install(tmp_path, "--model", FLINT, "prepare")
    assert code == 0, err
    assert f"Installing {FLINT} as Flint (gguf); draft incoai/Flint-DFlash2; vision disabled." in out
    installed = record(tmp_path, FLINT)
    assert installed["family"] == "Flint" and installed["vision_format"] == "none"
    assert not any(name.startswith("vision/") for name in installed["files"])


def test_flint_loads_from_a_gguf_only(tmp_path):
    code, _, err = install(tmp_path, "--model", "RecursiveSI/flint", "prepare")
    assert code == 1
    assert "Flint loads from a GGUF only" in err
    config = tmp_path / "config.json"
    # Flint's config is flat: its text model's keys at the top level.
    config.write_text(json.dumps(signatures.text_config(families.named("Flint"))))
    assert upstream.check_model("gguf", "none", config).name == "Flint"
    with pytest.raises(models.ModelError, match=r"^Flint loads from a GGUF only$"):
        upstream.check_model("mlx-affine", "none", config)


def test_ornith_is_the_122b_family_from_a_split_gguf_or_mlx(tmp_path):
    code, out, err = install(tmp_path, "--model", ORNITH, "prepare")
    assert code == 0, err
    assert "Selected 3 split files UD-Q4_K_XL/Ornith-1.5-122B-UD-Q4_K_XL-00001-of-00003.gguf" in out
    assert "as Qwen3.5-122B-A10B (gguf); draft incoai/Ornith-1.5-122B-DFlash2; vision enabled." in out
    code, out, err = install(tmp_path, "--model", ORNITH_MLX, "prepare")
    assert code == 0, err
    assert record(tmp_path, ORNITH_MLX)["family"] == "Qwen3.5-122B-A10B"


def test_the_35b_and_122b_share_a_model_type_and_differ_by_size(tmp_path):
    config = tmp_path / "config.json"
    for name in ("Qwen3.6-35B-A3B", "Qwen3.5-122B-A10B"):
        config.write_text(json.dumps({"text_config": signatures.text_config(families.named(name))}))
        assert upstream.check_model("mlx-affine", "none", config).name == name


def window_keys() -> set[str]:
    """The keys Status.cpp appendKvWindow writes under kv.window."""
    source = (ENGINE / "Status.cpp").read_text()
    body = source[source.index("void appendKvWindow") :]
    body = body[: body.index("\n}\n")]
    return set(re.findall(r'\\"([a-z_]+)\\":', body)) - {"window"}


def test_flint_reports_its_sliding_window_and_no_reasoning_switch():
    fake = engine(FLINT)
    run(fake, job(prompt=900))
    status = fake.status({})
    assert status["reasoning_switch"] is False
    assert status["vision"] is False and status["input_modalities"] == ["text"]
    window = status["kv"]["window"]
    assert window["window_pages"] == 16 and window["trim"] is True
    slim_page = status["memory_plan"]["budget"]["kv_window"]["slim_page_bytes"]
    assert window["slim_bytes"] == window["slim_pages"] * slim_page
    assert set(status["memory_plan"]["budget"]["kv_window"]) == {"slim_page_bytes", "floor_pages"}
    model = status["memory_plan"]["model"]
    assert (model["attention_layers"], model["kv_heads"], model["head_dimension"]) == (49, 4, 128)
    if ENGINE.exists():
        assert window_keys() == set(window), sorted(window_keys() ^ set(window))


def test_other_targets_keep_the_reasoning_switch_and_no_window():
    status = engine(DENSE).status({})
    assert status["reasoning_switch"] is True
    assert "window" not in status["kv"]
    assert "kv_window" not in status["memory_plan"]["budget"]
    assert not any(path.startswith("kv.window") for path in flatten(status))


def test_flint_serves_its_native_window_only():
    plan = engine(FLINT).plan
    assert plan is not None and plan.context_bounds["window"] == 262144 and plan.rope_scaling is None


def test_flint_decides_itself_whether_to_reason():
    fake = engine(FLINT)
    long = FakeJob("c", list(range(40)), "x", 64, last_user="why is the sky blue today", thinking=False)
    short = FakeJob("c", list(range(40)), "x", 64, last_user="hi", thinking=True)
    assert fake._output_plan(long)[0], "Flint reasons whatever enable_thinking says"
    assert not fake._output_plan(short)[0]


def test_flint_refuses_a_context_past_its_window(capsys):
    from server.fake_engine import StartupFailed

    fake = engine(FLINT, "--max-context", "300K")
    with pytest.raises(StartupFailed, match="native protocol reached EOF"):
        fake.start()
    assert "error: MAX_CONTEXT: Flint serves at most 262144 tokens of context" in capsys.readouterr().err
