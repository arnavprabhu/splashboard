"""Engine output lines → structured events. Lines are Splash's own (1.3.0 to 1.4.0)."""

from __future__ import annotations

from splash_gui.engine.startup import (
    ErrorEvent,
    NoticeEvent,
    PhaseEvent,
    ReadyEvent,
    StartupParser,
    TakenBackEvent,
    TemplateEvent,
    TransportEvent,
    WeightsEvent,
    parse_context,
    strip_stamp,
)
from splash_gui.models import catalog, compat

BUDGET = """error: runtime bootstrap failed [resource_assembly]: \
hard budget cannot fit one lane's state and the KV runway
engine memory budget validation failed [kv_pool_does_not_fit]: \
hard budget cannot fit one lane's state and the KV runway
physical memory: 25769803776 bytes (24576.00 MiB)
recommended working set: 19327352832 bytes (18432.00 MiB)
configured memory limit: 8589934592 bytes (8192.00 MiB)
working-set margin (max of 1 GiB or 2%): 1073741824 bytes (1024.00 MiB)
hard budget: 7516192768 bytes (7168.00 MiB)
target weights: 13000000000 bytes (12397.77 MiB)
deficit: 6000000000 bytes (5722.05 MiB)
memory_plan_json: {"valid":false,"maximum_context_tokens":0}"""


def feed_all(parser: StartupParser, text: str) -> list[object]:
    events: list[object] = []
    for line in text.splitlines():
        events += parser.feed(line)
    return events + parser.finish()


def test_strip_stamp_and_context() -> None:
    assert strip_stamp("22:52:58 Ready · x") == "Ready · x"
    assert strip_stamp("no stamp") == "no stamp"
    assert parse_context("256K") == 262144
    assert parse_context("100,000") == 100000
    assert parse_context("nope") is None


def test_startup_sequence() -> None:
    parser = StartupParser()
    events = feed_all(
        parser,
        "\n".join(
            [
                "Selected Qwen3.6-35B-A3B-UD-Q4_K_M.gguf from unsloth/Qwen3.6-35B-A3B-GGUF.",
                "Fetching 2 file(s), 0.00 GB, from unsloth/X@2874454a22ae; "
                "cached files are reused.",
                "22:52:58 Loading · unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M",
                "22:52:58 Chat template · patched to render later system messages in place "
                "(the original template rejects them), generation prompt 3-7 tokens",
                "22:52:58 Weights loaded in 0.05 s.",
                "22:52:58 Ready · unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M · context 128K · "
                "language only · http://127.0.0.1:49308",
            ]
        ),
    )
    phases = [e.phase for e in events if isinstance(e, PhaseEvent)]
    assert phases[:2] == ["installing", "installing"]
    assert "loading" in phases and phases[-1] == "warming"
    # Warming starts when the weights are in, not when the engine is ready.
    assert parser.feed("22:52:58 Weights loaded in 0.05 s.") == [PhaseEvent("warming")]
    assert TemplateEvent("patched") in events
    ready = next(e for e in events if isinstance(e, ReadyEvent))
    assert ready.context_tokens == 131072 and ready.language_only
    assert ready.model == "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M"


def test_template_modes() -> None:
    for text, mode in (
        (
            "Chat template · renders later system messages in place, generation prompt 3 tokens",
            "native",
        ),
        (
            "Chat template · requests with later system messages are rejected (the original "
            "template rejects them), generation prompt 3 tokens",
            "unsupported",
        ),
    ):
        assert StartupParser().feed(text) == [TemplateEvent(mode)]  # type: ignore[arg-type]


def test_budget_refusal_block_collects_breakdown() -> None:
    events = feed_all(StartupParser(), BUDGET)
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    error = errors[0].error
    assert error.kind == "budget_refusal" and error.code == "kv_pool_does_not_fit"
    assert error.budget is not None
    labels = [row.label for row in error.budget]
    assert labels[0] == "physical memory" and "deficit" in labels
    assert error.budget[0].bytes == 25769803776
    actions = [s.action for s in error.suggestions]
    assert actions == ["lower_max_context", "language_only", "raise_max_memory", "smaller_variant"]


def test_max_context_exceeds_suggests_the_cap() -> None:
    text = (
        "error: runtime bootstrap failed [model_creation]: --max-context 300000 exceeds the "
        "131072 tokens the model and this Mac's memory allow; omit it or pass at most 131072\n"
        "physical memory: 1 bytes (0.00 MiB)\n"
        "deficit: 0 bytes (0.00 MiB)"
    )
    error = next(e for e in feed_all(StartupParser(), text) if isinstance(e, ErrorEvent)).error
    assert error.code == "max_context_exceeds"
    assert error.suggestions[0].patch == {"serve.max_context": "131072"}
    assert error.budget and len(error.budget) == 2


def test_errors_are_classified() -> None:
    gated = (
        "error: cannot install meta/x: 401 Client Error. Cannot access gated repo for url "
        "https://huggingface.co/api/models/meta/x; set HF_TOKEN or run 'hf auth login' with access"
    )
    cases = {
        gated: "gated",
        "error: cannot install a/b: no supported model has this architecture "
        "(hidden_size=4096); "
        "supported: Qwen3.8-27B, Qwen3.6-35B-A3B": "incompatible",
        "error: cannot install a/b: this model requires an MLX checkpoint (affine 2, 3, 4, 5, "
        "6 or 8 bits in groups of 32, 64 or 128, or mxfp4) or a supported GGUF": "incompatible",
        "error: incoai/Qwen3.8-27B-Splash is a Splash package, which Splash no longer loads; "
        "serve the MLX model of its family instead: splash serve --model "
        "mlx-community/Qwen3.8-27B-4bit": "incompatible",
        "error: Splash is already serving (PID 1, model m, port 8000); "
        "stop it with Ctrl+C first": "port_in_use",
        "error: cannot install a/b: 404 Client Error. Repository Not Found": "unknown_model",
        "error: something odd happened": "other",
    }
    for line, kind in cases.items():
        events = StartupParser().feed(line)
        assert len(events) == 1 and isinstance(events[0], ErrorEvent), line
        assert events[0].error.kind == kind, (line, events[0].error)
    seven_bit = (
        "error: cannot install a/b: quantization language_model.model.embed_tokens is affine "
        "7-bit in groups of 64; MLX weights load as affine 2, 3, 4, 5, 6 or 8 bits in groups "
        "of 32, 64 or 128, or as mxfp4"
    )
    refused = StartupParser().feed(seven_bit)[0]
    assert isinstance(refused, ErrorEvent)
    # The plain line leads; the engine's words stay in `raw`.
    assert refused.error.kind == "incompatible"
    assert refused.error.message == compat.MLX_QUANTIZATION
    assert refused.error.raw == [seven_bit]
    gated_error = StartupParser().feed(gated)[0]
    assert isinstance(gated_error, ErrorEvent)
    assert gated_error.error.message == "This model requires a Hugging Face token"
    assert gated_error.error.suggestions[0].action == "add_hf_token"


def test_splash_1_3_2_refusals_are_incompatible() -> None:
    whole_bf16 = (
        "error: cannot install a/b-GGUF: this GGUF stores every projection and expert "
        "unquantized (BF16, F16 or F32): Splash loads quantized GGUFs, not whole-BF16 or F16 "
        "models; choose a quantized variant of the repository"
    )
    model_optimizer = (
        "error: cannot install nvidia/x: quantization_config is INT4_AWQ; Model Optimizer "
        "weights load as NVFP4, W4A16_NVFP4 or FP8"
    )
    unquantized_head = (
        "error: cannot install nvidia/x: quantization_config leaves lm_head unquantized; "
        "Splash loads quantized projections"
    )
    for line, message in (
        (whole_bf16, catalog.WHOLE_UNQUANTIZED),
        (model_optimizer, compat.CHECKPOINT_QUANTIZATION),
        (unquantized_head, compat.CHECKPOINT_UNQUANTIZED),
    ):
        event = StartupParser().feed(line)[0]
        assert isinstance(event, ErrorEvent), line
        assert event.error.kind == "incompatible" and event.error.message == message, line
        assert event.error.raw == [line]
    gguf = StartupParser().feed(whole_bf16)[0]
    assert isinstance(gguf, ErrorEvent)
    assert [s.action for s in gguf.error.suggestions] == ["smaller_variant"]


def test_a_thinking_end_text_the_tokenizer_refuses_points_at_the_setting() -> None:
    for line in (
        "12:00:00 Error · --thinking-end-text must end with </think> and hold no other "
        "special token",
        "12:00:00 Error · --thinking-end-text takes 80 tokens; the most is 64",
    ):
        event = StartupParser().feed(line)[0]
        assert isinstance(event, ErrorEvent), line
        assert event.error.code == "invalid_setting"
        assert "serve.thinking_end_text" in event.error.message


def test_weights_released_on_request_is_a_release() -> None:
    assert StartupParser().feed("Weights released on request; the next request restores them") == [
        WeightsEvent("released")
    ]


def test_request_error_lines_are_not_startup_errors() -> None:
    assert StartupParser().feed("22:53:00 Error · capacity_exhausted · POST /v1/chat") == []


def test_notices_transport_and_weights() -> None:
    parser = StartupParser()
    off = (
        "SSD cache off: the memory plan holds the context only without it; "
        "no state staging is set aside."
    )
    assert parser.feed(f"22:52:58 {off}") == [NoticeEvent("disk_cache_off", off)]
    temporary = (
        "Persistent cache off: its root is not on a local volume; "
        "the SSD cache keeps temporary files."
    )
    assert parser.feed(temporary) == [NoticeEvent("persistent_cache_off", temporary)]
    # The SSD cache's own startup line is not a notice.
    assert parser.feed("SSD cache: up to 32 GiB of temporary files") == []
    hub = parser.feed("Could not reach the Hub (timeout); using the installed a/b@123456789012.")
    assert isinstance(hub[0], NoticeEvent) and hub[0].kind == "hub_unreachable"
    kept = parser.feed("Warning: keeping the installed a/b@123; cannot install a/b@456: boom")
    assert isinstance(kept[0], NoticeEvent) and kept[0].kind == "new_commit_not_installed"
    assert parser.feed("12:00:00 Engine failed · native protocol reached EOF") == [
        TransportEvent("failed", "native protocol reached EOF")
    ]
    assert parser.feed("12:00:01 Engine restarted")[0] == TransportEvent(
        "restarted", "Engine restarted"
    )
    assert isinstance(parser.feed("12:00:02 Engine stopped · gave up")[0], TransportEvent)
    assert parser.feed(
        "Weights released after 600 s without a request; the next request restores them"
    ) == [WeightsEvent("released")]
    assert parser.feed("Weights restored in 1.25 s") == [WeightsEvent("restored", 1.25)]
    assert parser.feed(
        "12:00:00 Persistent cache /x/fake: took back 3 restore points over 40 KV blocks "
        "(128 MiB); left 1 copies behind."
    ) == [TakenBackEvent(3, 40, 128, 1)]


def test_unknown_lines_produce_nothing() -> None:
    parser = StartupParser()
    assert parser.feed("22:52:58 Kernel policy for GPU family 10 with 20 cores.") == []
    assert parser.feed("22:52:58 Done · input 5 · cached 0 · output 5") == []
    assert parser.feed("") == []


# --- Splash 1.4.0 -----------------------------------------------------------


def test_a_store_setting_refusal_is_a_settings_error_not_a_memory_one() -> None:
    """The planner refuses an explicit store size before the weights load, in the
    same [resource_assembly/…] form as a budget refusal (MemoryPlan.cpp resolve)."""
    for line, key in (
        (
            "error: runtime bootstrap failed [resource_assembly/configuration]: --expert-memory of "
            "100 MiB is less than its smallest store, 448 MiB",
            "serve.expert_memory",
        ),
        (
            "error: runtime bootstrap failed [resource_assembly/configuration]: --ple-memory "
            "cannot stream the PLE table: its rows are not one format",
            "serve.ple_memory",
        ),
        (
            "error: runtime bootstrap failed [resource_assembly/configuration]: --sparse-kv-memory "
            "of 9000 MiB is more than the hot block cache's floor, 390 MiB, and the 2048 MiB the "
            "floors leave",
            "serve.sparse_kv_memory",
        ),
    ):
        events = feed_all(StartupParser(), line)
        (event,) = [e for e in events if isinstance(e, ErrorEvent)]
        error = event.error
        assert (error.kind, error.code) == ("other", "invalid_setting")
        assert error.message.startswith("Settings · ") and f"({key}): --" in error.message
        assert error.budget is None
        reset = error.suggestions[0]
        assert (reset.action, reset.patch) == ("reset_setting", {key: "auto"})


def test_a_store_refusal_from_the_plan_after_the_load_keeps_its_settings_fix() -> None:
    """The plan made with the loaded arenas refuses a store at memory_planning, and
    Bootstrap.mm describe() then prints the same message as a budget block."""
    text = (
        "error: runtime bootstrap failed [resource_assembly/memory_planning]: --sparse-kv-memory "
        "of 9000 MiB is more than the hot block cache's floor, 390 MiB, and the 2048 MiB the "
        "floors leave\n"
        "engine memory budget validation failed [kv_pool_does_not_fit]: --sparse-kv-memory of "
        "9000 MiB is more than the hot block cache's floor, 390 MiB, and the 2048 MiB the floors "
        "leave\nhard budget: 7516192768 bytes (7168.00 MiB)\nmodel weights: 5000000000 bytes"
    )
    errors = [e.error for e in feed_all(StartupParser(), text) if isinstance(e, ErrorEvent)]
    assert [(e.kind, e.code) for e in errors] == [("other", "invalid_setting")]
    assert errors[0].suggestions[0].patch == {"serve.sparse_kv_memory": "auto"}


def test_a_real_capacity_refusal_with_streamed_stores_stays_a_budget_refusal() -> None:
    line = (
        "error: runtime bootstrap failed [resource_assembly/memory_planning]: model weights with "
        "the runtime reserves, one lane's state, the KV runway, the streamed stores at their "
        "floors and any SSD cache state staging require 40000000000 bytes but the Metal memory "
        "budget is 30000000000 bytes"
    )
    (event,) = [e for e in feed_all(StartupParser(), line) if isinstance(e, ErrorEvent)]
    assert (event.error.kind, event.error.code) == ("budget_refusal", "kv_pool_does_not_fit")


def test_a_context_the_ssd_cache_bounds_suggests_the_cache_not_memory() -> None:
    text = (
        "error: runtime bootstrap failed [model_creation]: --max-context 1000000 exceeds the "
        "160000 tokens the model and the SSD cache's room for its KV allow; omit it or pass at "
        "most 160000\nhard budget: 7516192768 bytes (7168.00 MiB)"
    )
    (event,) = [e for e in feed_all(StartupParser(), text) if isinstance(e, ErrorEvent)]
    error = event.error
    assert error.code == "max_context_exceeds"
    assert "SSD cache's room" in error.message and "1,000,000" in error.message
    actions = [(s.action, s.patch) for s in error.suggestions]
    assert actions == [
        ("lower_max_context", {"serve.max_context": "160000"}),
        ("enable_ssd_cache", {"serve.max_cache_disk": "auto"}),
    ]
    memory = (
        "error: runtime bootstrap failed [model_creation]: --max-context 900000 exceeds the "
        "500000 tokens the model and --max-memory allow; omit it or pass at most 500000"
    )
    (event,) = [e for e in feed_all(StartupParser(), memory) if isinstance(e, ErrorEvent)]
    assert "the memory ceiling allows" in event.error.message
    assert any(s.action == "language_only" for s in event.error.suggestions)


def test_new_startup_notices() -> None:
    parser = StartupParser()
    for line, kind in (
        (
            "12:00:00 --ple-memory ignored: Qwen3.6-35B-A3B has no PLE table to stream",
            "setting_ignored",
        ),
        (
            "12:00:00 --sparse-kv-memory ignored: Qwen3.8-Flash-Next keeps its KV in memory, the "
            "SSD cache is off.",
            "setting_ignored",
        ),
        (
            "12:00:00 YaRN is on: the target's RoPE is scaled by 3.8147 for 1000000 tokens of "
            "context, past the native window of 262144. Static YaRN changes answers at every "
            "length, so pass a --max-context above 262144 only when a request needs it.",
            "yarn_on",
        ),
        (
            "12:00:00 Persistent cache emptied: the last two starts failed within their "
            "probation, or the last one's engine did.",
            "persistent_cache_emptied",
        ),
    ):
        (event,) = parser.feed(line)
        assert isinstance(event, NoticeEvent) and event.kind == kind
        assert event.message == strip_stamp(line)
    # Lines that say how a store or the verify rows were set up are informational.
    for line in (
        "12:00:00 Routed experts stream from their files: 1820 slots for 10240 experts (17%) in "
        "16380 MiB.",
        "12:00:00 Decode cycles verify every row of the draft's block (--verify-rows all).",
        "12:00:00 Long requests keep their KV on the SSD cache: 262144 tokens of context (bound: "
        "model; window 262144, resident 65536, spilled 262144, disk 900000); a request whose KV "
        "fits 2048 MiB stays in memory; hot block cache 390 MiB (floor 390 MiB); history staging "
        "in 2 buffers; SSD room for active KV 180.0 GiB.",
    ):
        assert parser.feed(line) == []


def test_a_reload_and_the_new_took_back_suffix() -> None:
    parser = StartupParser()
    assert parser.feed("12:00:00 Weights loaded in 4.10 s.") == [PhaseEvent("warming")]
    assert parser.feed("12:00:00 Weights loaded again in 3.90 s.") == []
    (taken,) = parser.feed(
        "12:00:00 Persistent cache /c/abc: took back 3 restore points over 40 KV blocks (12 MiB); "
        "left 0 copies behind. The last process did not stop cleanly."
    )
    assert taken == TakenBackEvent(3, 40, 12, 0)


def test_a_million_token_ready_line() -> None:
    (_, ready) = StartupParser().feed(
        "12:00:00 Ready · unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M · context 1,000,000 · "
        "http://127.0.0.1:18000"
    )
    assert isinstance(ready, ReadyEvent) and ready.context_tokens == 1_000_000


def test_an_unprepared_model_is_its_own_error() -> None:
    for line in (
        "12:00:00 Error · /Users/me/.splash/models/.links/abc names no model Splash prepared: "
        "prepare the model again",
        "12:00:00 Error · /tmp/x is not a model directory Splash prepared",
        "12:00:00 Error · cannot hold /m/.links/a in /m: [Errno 13] Permission denied",
    ):
        (event,) = StartupParser().feed(line)
        assert isinstance(event, ErrorEvent) and event.error.code == "model_not_prepared", line
        assert "Models page" in event.error.message


def test_a_fix_that_repeats_the_refused_settings_is_dropped() -> None:
    """With the SSD cache already auto, "Let the SSD cache follow the disk" would
    start the same way; only the context fix is left."""
    from types import SimpleNamespace

    from splash_gui.engine.supervisor import Supervisor
    from splash_gui.settings.model import SettingsDocument

    text = (
        "error: runtime bootstrap failed [model_creation]: --max-context 1000000 exceeds the "
        "160000 tokens the model and the SSD cache's room for its KV allow; omit it or pass at "
        "most 160000\nhard budget: 7516192768 bytes (7168.00 MiB)"
    )
    (event,) = [e for e in feed_all(StartupParser(), text) if isinstance(e, ErrorEvent)]
    owner = SimpleNamespace(
        model="unsloth/Qwen3.8-Flash-Next-GGUF:Q2_0",
        app=SimpleNamespace(settings=SimpleNamespace(current=SettingsDocument())),
    )
    kept = Supervisor._without_noop_fixes(owner, event.error)  # type: ignore[arg-type]
    assert [s.action for s in kept.suggestions] == ["lower_max_context"]
