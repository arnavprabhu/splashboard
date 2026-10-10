"""Engine output lines → structured events. Lines are Splash 1.3.0's own."""

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
    assert refused.error.message == (
        "Splash runs MLX models quantized as affine 2, 3, 4, 5, 6 or 8 bits "
        "(groups of 32, 64 or 128) or as mxfp4."
    )
    assert refused.error.raw == [seven_bit]
    gated_error = StartupParser().feed(gated)[0]
    assert isinstance(gated_error, ErrorEvent)
    assert gated_error.error.message == "This model requires a Hugging Face token"
    assert gated_error.error.suggestions[0].action == "add_hf_token"


def test_request_error_lines_are_not_startup_errors() -> None:
    assert StartupParser().feed("22:53:00 Error · capacity_exhausted · POST /v1/chat") == []


def test_notices_transport_and_weights() -> None:
    parser = StartupParser()
    assert parser.feed(
        "22:52:58 The 40960 MiB this Mac had available at startup may not hold a 262144-token "
        "request; one that runs out of memory is suspended and replays its prompt. "
        "--max-cache-disk SIZE keeps its progress and cached prefixes on SSD."
    )[0] == NoticeEvent(
        "disk_tier_suggestion",
        "The 40960 MiB this Mac had available at startup may not hold a 262144-token request; "
        "one that runs out of memory is suspended and replays its prompt. --max-cache-disk SIZE "
        "keeps its progress and cached prefixes on SSD.",
    )
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
