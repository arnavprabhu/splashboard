"""Behaviour Splash 1.4.0 changed on the serve side that the GUI reads: contexts
past the native window (YaRN), fractional sizes, the streamed stores' and
--verify-rows' startup lines and refusals, KV on the SSD cache, an explicit SSD
cache size held as given, the server holding the model's assembly, and an
error's Retry-After."""

from __future__ import annotations

import re

from test_startup_and_signals import TS, run_cli

from conftest import chat_body

GIB = 1 << 30
DENSE = "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL"
FLASH_NEXT = "ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF:Q2_0"
# A model the fake installer may not publish runs without it.
NO_INSTALL = {"FAKE_SPLASH_SKIP_INSTALL": "1"}


def _failed(fake, timeout: float = 20.0) -> int:
    """Wait for a start that fails, and return its exit code."""
    code = fake.process.wait(timeout)
    fake.wait_for_line(r"Error · ", stream="stderr")
    return code


# --- options ----------------------------------------------------------------


def test_the_serve_options_parse_as_splash_1_4_0_does(tmp_path):
    for args, message in (
        (["--max-context", "1010001"], "must be 'auto' or a token count up to 1010000, such as 100K or 1M"),
        (["--max-memory", "-5"], "must be 'auto' or a positive byte count such as 32G or 2.5G"),
        (["--ple-memory", "lots"], "use auto, off, or a size such as 2G"),
    ):
        result = run_cli(tmp_path, "serve", "--model", "unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M", *args)
        assert result.returncode == 2 and message in result.stderr, result.stderr


def test_a_million_tokens_turn_on_yarn(make_engine):
    fake = make_engine(args=["--no-webui", "--max-context", "1M"])
    fake.wait_for_line(
        TS + r"YaRN is on: the target's RoPE is scaled by 3\.8147 for 1000000 tokens of context, past the native "
        r"window of 262144\. Static YaRN changes answers at every length, so pass a --max-context above 262144 "
        r"only when a request needs it\.$",
        stream="stderr",
    )
    # server.py prints a context that is no whole K with commas.
    fake.wait_for_line(TS + r"Ready · \S+ · context 1,000,000 · ", stream="stdout")


def test_512k_is_twice_the_native_window(make_engine):
    fake = make_engine(args=["--no-webui", "--max-context", "512K"])
    fake.wait_for_line(r"scaled by 2 for 524288 tokens of context", stream="stderr")
    fake.wait_for_line(TS + r"Ready · \S+ · context 512K · ", stream="stdout")
    assert not any("YaRN" in line for line in make_engine(args=["--no-webui", "--max-context", "256K"]).stderr)


def test_a_fractional_size_is_taken(make_engine):
    fake = make_engine(args=["--no-webui", "--max-memory", "40.5G"])
    plan = fake.json("GET", "/status")[1]["memory_plan"]["budget"]
    assert plan["configured_memory_limit_bytes"] == 40 * GIB + GIB // 2


# --- the streamed stores and verify rows ------------------------------------


def test_verify_rows_all_and_dense_targets(make_engine):
    moe = make_engine(args=["--no-webui", "--verify-rows", "all"])
    moe.wait_for_line(
        TS + r"Decode cycles verify every row of the draft's block \(--verify-rows all\)\.$", stream="stderr"
    )
    dense = make_engine(model=DENSE, env=NO_INSTALL)
    dense.wait_for_line(
        TS + r"Decode cycles verify every row of the draft's block \(--verify-rows auto, as Qwen3\.8-27B is "
        r"dense\)\.$",
        stream="stderr",
    )
    assert not any("Routed experts" in line for line in dense.stderr)


def test_an_expert_memory_size_streams_the_experts(make_engine):
    fake = make_engine(args=["--no-webui", "--expert-memory", "8G"])
    line = fake.wait_for_line(r"Routed experts stream from their files: ", stream="stderr")
    assert re.search(r": \d+ slots for 10240 experts \(\d+%\) in \d+ MiB\.$", line), line


def test_a_store_for_a_part_the_model_lacks_is_ignored(make_engine):
    fake = make_engine(args=["--no-webui", "--ple-memory", "2G"])
    fake.wait_for_line(TS + r"--ple-memory ignored: Qwen3\.6-35B-A3B has no PLE table to stream$", stream="stderr")
    dense = make_engine(model=DENSE, env=NO_INSTALL, args=["--no-webui", "--expert-memory", "4G"])
    dense.wait_for_line(r"--expert-memory ignored: Qwen3\.8-27B has no routed experts to stream$", stream="stderr")


def test_a_store_below_its_floor_is_refused_before_the_weights_load(make_engine):
    fake = make_engine(args=["--no-webui", "--expert-memory", "100M"], wait=False)
    assert _failed(fake) == 1
    assert any(
        line == "error: runtime bootstrap failed [resource_assembly/configuration]: --expert-memory of 100 MiB "
        "is less than its smallest store, 448 MiB"
        for line in fake.stderr
    ), fake.stderr
    assert not any("Weights loaded" in line for line in fake.stderr)


def test_a_configuration_refusal_on_request(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_FAIL_STARTUP": "configuration"}, wait=False)
    assert _failed(fake) == 1
    assert any("[resource_assembly/configuration]: --expert-memory of 256 MiB" in line for line in fake.stderr)


def test_the_weights_can_load_again(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_LOAD_AGAIN": "1"})
    fake.wait_for_line(TS + r"Weights loaded again in \d+\.\d\d s\.$", stream="stderr")


# --- Qwen3.8-Flash-Next: streamed table and experts, KV on the SSD cache ------


def test_flash_next_streams_and_keeps_long_requests_kv_on_the_ssd(make_engine):
    fake = make_engine(model=FLASH_NEXT, env=NO_INSTALL)
    fake.wait_for_line(
        r"Table per_layer_token_embd\.weight streams from its file: \d+ MiB stay on the SSD; row cache of \d+ rows "
        r"in 512 MiB, all that --ple-memory auto leaves it beside the floors and the KV kept resident; 32 readers\.$",
        stream="stderr",
    )
    fake.wait_for_line(r"Routed experts stream from their files: .*--expert-memory auto", stream="stderr")
    fake.wait_for_line(r"as Qwen3\.8-Flash-Next is a MoE target", stream="stderr")
    line = fake.wait_for_line(r"Long requests keep their KV on the SSD cache: ", stream="stderr")
    assert re.search(r"\(bound: (model|memory|disk); window 262144, resident \d+, spilled 262144, disk \d+\)", line)
    assert not any("Neural Engine" in line or "prefill FFN" in line for line in fake.stderr)


def test_flash_next_without_the_ssd_cache_ignores_sparse_kv_memory(make_engine):
    fake = make_engine(
        model=FLASH_NEXT, env=NO_INSTALL, args=["--no-webui", "--max-cache-disk", "0", "--sparse-kv-memory", "1G"]
    )
    fake.wait_for_line(
        TS + r"Long requests keep their KV in memory, the SSD cache is off: \d+ tokens of context\.$", stream="stderr"
    )
    fake.wait_for_line(
        TS + r"--sparse-kv-memory ignored: Qwen3\.8-Flash-Next keeps its KV in memory, the SSD cache is off\.$",
        stream="stderr",
    )


def test_a_sparse_kv_memory_below_its_floor_is_refused(make_engine):
    fake = make_engine(model=FLASH_NEXT, env=NO_INSTALL, args=["--no-webui", "--sparse-kv-memory", "100M"], wait=False)
    assert _failed(fake) == 1
    assert any(
        "[resource_assembly/configuration]: --sparse-kv-memory of 100 MiB is less than the hot block cache's "
        "floor, 390 MiB" in line
        for line in fake.stderr
    )


def test_a_context_the_ssd_cache_cannot_hold_names_it(make_engine):
    fake = make_engine(
        model=FLASH_NEXT,
        env={**NO_INSTALL, "FAKE_SPLASH_VOLUME_AVAILABLE": "12G", "FAKE_SPLASH_PHYSICAL_MEMORY": "32G"},
        args=["--no-webui", "--max-context", "1M"],
        wait=False,
    )
    assert _failed(fake) == 1
    refusal = next(line for line in fake.stderr if "exceeds the" in line)
    assert re.match(
        r"error: runtime bootstrap failed \[model_creation\]: --max-context 1000000 exceeds the (\d+) tokens the "
        r"model and the SSD cache's room for its KV allow; omit it or pass at most \1$",
        refusal,
    ), refusal


# --- the SSD cache, the assembly and Retry-After ----------------------------


def test_an_explicit_ssd_cache_size_is_held_as_given(make_engine):
    fake = make_engine(args=["--no-webui", "--max-cache-disk", "64G"], env={"FAKE_SPLASH_VOLUME_AVAILABLE": "30G"})
    fake.wait_for_line(
        r"SSD cache: up to 20\.0 GiB of temporary files in \S+ \(the 20\.0 GiB its volume has beyond the 10\.0 GiB "
        r"it keeps free\)",
        stream="stderr",
    )
    auto = make_engine(env={"FAKE_SPLASH_VOLUME_AVAILABLE": "30G"})
    auto.wait_for_line(r"SSD cache: up to 5\.0 GiB .*\(a quarter of the 20\.0 GiB its volume has", stream="stderr")


def test_an_unprepared_model_stops_the_start(make_engine):
    fake = make_engine(env={"FAKE_SPLASH_FAIL_STARTUP": "unprepared"}, wait=False)
    assert _failed(fake) == 1
    assert any(
        re.match(TS + r"Error · \S+ names no model Splash prepared: prepare the model again$", line)
        for line in fake.stderr
    ), fake.stderr


def test_an_error_carries_its_retry_after(make_engine):
    fake = make_engine()
    fake.set_mode("kv_write_budget")
    status, headers, _ = fake.request("POST", "/v1/chat/completions", chat_body())
    assert status == 503 and headers["Retry-After"] == "3600"
    fake.set_mode("queue_full")
    status, headers, _ = fake.request("POST", "/v1/chat/completions", chat_body())
    assert status == 503 and headers["Retry-After"] == "1"
