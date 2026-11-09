"""Splash 1.6.0 in the manager: /status schema 8 and the context the server serves
now, the installer's own account of a failed download, free space as Finder counts it,
a split GGUF at a repository's root and the new :VARIANT order, the reworded start
refusals, a Chat request's token count through the proxy, and a quarantined request."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from splash_gui.downloads.service import InstallerFailed, installer_failure
from splash_gui.engine import discovery
from splash_gui.engine import status as st
from splash_gui.engine.startup import classify_error
from splash_gui.models import catalog as cat
from splash_gui.models import compat
from splash_gui.system import diskspace

from .fakeengine import MODEL, EngineHarness
from .test_downloads import hub_harness, queue, wait_for  # noqa: F401  (fixture)

GIB = 1024**3


# --- version and status -----------------------------------------------------------------------


def test_the_supported_range_is_1_6() -> None:
    assert discovery.SUPPORTED_RANGE == ">=1.6.0 <1.7.0"
    assert discovery.support_for((1, 6, 0)) == "supported"
    assert discovery.support_for((1, 5, 0)) == "too_old"
    assert discovery.support_for((1, 7, 0)) == "untested"


def test_schema_8_is_the_status_the_manager_knows(harness_factory) -> None:
    assert st.SCHEMA_VERSION == 8
    harness: EngineHarness = harness_factory()
    harness.load()
    status = harness.client.get("/status").json()
    assert status["schema_version"] == 8
    # The two fields 1.6.0 removed are gone, and its ledger is there.
    assert "resident_kv_bytes" not in status["memory_plan"]["budget"]
    assert status["memory_governor"]["claims"] == {"waiting": 0, "bytes": 0, "oldest_ms": 0.0}
    engine = harness.engine()
    assert engine["schema_supported"] is True
    alerts = harness.client.get("/api/admin/alerts").json()["alerts"]
    assert not any(a["condition"] == "schema_unrecognized" for a in alerts), alerts


def test_the_context_is_the_one_the_server_serves_now(harness_factory) -> None:
    """A relaunched engine may serve another context than the first plan allowed;
    `/status.capabilities.max_context_tokens` says which (frontend.py, 1.6.0), and the
    supervisor reads it before the plan's own figure."""
    harness: EngineHarness = harness_factory()
    harness.load()
    status = harness.client.get("/status").json()
    assert status["capabilities"]["max_context_tokens"] == status["maximum_context_tokens"]
    assert harness.engine()["maximum_context_tokens"] == status["maximum_context_tokens"]
    assert (
        st.integer(
            {"capabilities": {"max_context_tokens": 65536}}, "capabilities.max_context_tokens"
        )
        == 65536
    )


# --- downloads --------------------------------------------------------------------------------


def test_the_installer_s_error_event() -> None:
    line = json.dumps({"event": "error", "kind": "auth", "message": "cannot resolve o/r: 401"})
    assert installer_failure(line) == ("auth", "cannot resolve o/r: 401")
    plain = json.dumps({"event": "error", "kind": None, "message": "not enough disk space"})
    assert installer_failure(plain) == (None, "not enough disk space")
    for other in (
        '{"event": "download", "repo": "o/r", "done": 1, "total": 2}',
        '{"event": "error", "kind": "auth"}',
        "error: cannot resolve o/r",
        "{not json",
    ):
        assert installer_failure(other) is None, other


def test_a_failure_is_classed_by_splash_s_kind_before_its_words(harness_factory) -> None:
    downloads = harness_factory(installed=()).state.downloads
    item = type("Item", (), {"bytes_total": 10, "bytes_done": 0})()
    # A gated repository's refusal that happens to name no status code.
    assert downloads.classify(item, "cannot resolve o/r: access denied", "auth").code == "gated"
    assert downloads.classify(item, "cannot resolve o/r: timed out", "offline").code == (
        "hub_unreachable"
    )
    missing = downloads.classify(item, "cannot resolve o/r: Repository not found", "not_found")
    assert missing.code == "not_found" and missing.action is None
    # Without a kind the words decide, as before.
    assert downloads.classify(item, "401 Client Error", None).code == "gated"
    assert InstallerFailed("x", "auth").kind == "auth"


def test_a_gated_download_shows_no_json_in_its_log(hub_harness) -> None:  # noqa: F811
    """The installer says why as an event beside its `error:` line; the event classes
    the failure and stays out of the log the Downloads page shows."""
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "gated"})
    started = queue(harness, MODEL)
    failed = wait_for(harness, started["id"], "failed")
    assert failed["error"]["code"] == "gated" and failed["error"]["action"] == "add_hf_token"
    assert not any(line.startswith("{") for line in failed["log_tail"]), failed["log_tail"]
    assert any(line.startswith("error: cannot resolve") for line in failed["log_tail"])


def test_a_download_with_no_room_is_refused_by_splash_before_it_starts(hub_harness) -> None:  # noqa: F811
    """Splash 1.6.0 refuses a download its volume has no room for before it fetches a
    byte (install/hub.py `_require_room`): the job fails with the disk code."""
    harness = hub_harness({"FAKE_SPLASH_DL_FAIL": "no_room"})
    started = queue(harness, MODEL)
    failed = wait_for(harness, started["id"], "failed")
    assert failed["error"]["code"] == "disk_full" and failed["error"]["action"] == "free_space"
    assert "not enough disk space" in failed["error"]["message"]


def test_free_space_is_the_larger_of_finder_s_and_statvfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage._replace(free=5 * GIB))
    monkeypatch.setattr(diskspace, "_important_usage_bytes", lambda _path: 40 * GIB)
    assert diskspace.free_bytes(tmp_path) == 40 * GIB, "purgeable space counts, as in Finder"
    monkeypatch.setattr(diskspace, "_important_usage_bytes", lambda _path: 0)
    assert diskspace.free_bytes(tmp_path) == 5 * GIB, "a volume Finder does not show"


def test_a_download_macos_can_make_room_for_is_not_refused(
    hub_harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = hub_harness()
    usage = shutil.disk_usage(harness.home)
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage._replace(free=10**6))
    refused = harness.client.post("/api/admin/downloads", json={"id": MODEL})
    assert refused.status_code == 507 and refused.json()["error"]["code"] == "disk_full"
    monkeypatch.setattr(diskspace, "_important_usage_bytes", lambda _path: 200 * GIB)
    assert harness.client.post("/api/admin/downloads", json={"id": MODEL}).status_code == 201


# --- variants ---------------------------------------------------------------------------------


def test_a_root_file_that_only_ends_in_the_variant_no_longer_hides_its_folder() -> None:
    head = "mtp-Qwen3.8-Flash-Next-Q8_0.gguf"
    folder = [f"Q8_0/Qwen3.8-Flash-Next-Q8_0-0000{k}-of-00002.gguf" for k in (1, 2)]
    files: dict[str, int | None] = dict.fromkeys([head, *folder, "mmproj-BF16.gguf"], GIB)
    assert cat.root_choice(files, "Q8_0") is None
    [variant] = cat.gguf_variants("unsloth/Qwen3.8-Flash-Next-GGUF", files)
    assert variant.name == "Q8_0" and variant.unloadable is None and variant.files == folder
    # A root name that is the variant exactly still comes first, a split one too.
    exact = dict.fromkeys(["M-Q4_K_M.gguf", "M-Q8_0-00001-of-00002.gguf", *folder], GIB)
    assert cat.root_choice(exact, "Q8_0") == "M-Q8_0-00001-of-00002.gguf"
    assert cat.root_choice([*exact, "M-Q8_0.gguf"], "Q8_0") == "M-Q8_0.gguf"


# --- start refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "Error · the thinking end 'So, the answer:' must end with </think> and hold no "
        "other special token",
        "Error · the thinking end 'a long sentence</think>' takes 70 tokens; the most is 64",
    ],
)
def test_a_refused_thinking_end_names_the_setting(line: str) -> None:
    error = classify_error(line)
    assert error is not None and error.code == "invalid_setting"
    assert "serve.thinking_end_text" in error.message


def test_a_whole_unquantized_gguf_is_refused_in_1_6_0_s_words() -> None:
    reason = (
        "this GGUF stores its weights unquantized (BF16, F16 or F32): Splash loads quantized "
        "GGUFs, not whole-BF16 or F16 models; choose a quantized variant of the repository"
    )
    assert compat.explain_refusal(reason) == (cat.WHOLE_UNQUANTIZED, reason)
    error = classify_error(f"error: cannot install a/b-GGUF: {reason}")
    assert error is not None and error.code == "incompatible"
    assert error.message == cat.WHOLE_UNQUANTIZED


# --- proxy ------------------------------------------------------------------------------------


def test_a_chat_request_s_tokens_are_counted_without_running_it(harness_factory) -> None:
    harness: EngineHarness = harness_factory()
    harness.load()
    body = {"model": MODEL, "messages": [{"role": "user", "content": "How long is this?"}]}
    before = harness.client.get("/status").json()["requests"]["submitted"]
    response = harness.client.post("/v1/chat/completions/count_tokens", json=body)
    assert response.status_code == 200, response.text
    assert response.json().keys() == {"input_tokens"} and response.json()["input_tokens"] > 0
    assert harness.client.get("/status").json()["requests"]["submitted"] == before
    # Errors are OpenAI's shape on this path, not Messages'.
    missing = harness.client.post(
        "/v1/chat/completions/count_tokens", json={**body, "model": "nobody/nothing"}
    )
    assert missing.status_code == 404 and "error" in missing.json()


def test_a_quarantined_request_raises_its_alert(harness_factory) -> None:
    harness: EngineHarness = harness_factory()
    harness.load()
    harness.fake("POST", "/_fake/mode", {"mode": "request_quarantined"})
    response = harness.client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "request_quarantined"
    alerts = harness.client.get("/api/admin/alerts").json()["alerts"]
    [alert] = [a for a in alerts if a["condition"] == "request_quarantined"]
    assert "refused" in alert["title"] and "restart the engine" in alert["message"]
