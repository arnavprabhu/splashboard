"""`splash pull` progress on a TTY draws the overall bar and updates it in place (docs/ui/11
§6.2, §2.2); a non-TTY keeps the timestamped lines (§6.3)."""

from __future__ import annotations

import io
import sys
import types
from typing import Any

import pytest

from splash_gui import cli as cli_module
from splash_gui.cli.output import Style


def test_the_bar_fills_by_the_ratio_in_utf8_and_in_ascii() -> None:
    assert Style(utf8=True).bar(0.5, 10) == "█████░░░░░"
    assert Style(utf8=False).bar(0.5, 10) == "#####-----"
    assert Style().bar(0.0, 4) == "░░░░"
    assert Style().bar(1.0, 4) == "████"


def test_the_bar_clamps_and_an_unknown_ratio_is_empty() -> None:
    assert Style().bar(2.0, 4) == "████"
    assert Style().bar(-1, 4) == "░░░░"
    assert Style().bar(None, 4) == "░░░░"
    assert Style().bar(float("nan"), 4) == "░░░░"


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeClient:
    """The manager's download answers: POST queues the item, GET polls it."""

    def __init__(self, polls: list[dict[str, Any]]) -> None:
        self.polls = polls
        self.http = types.SimpleNamespace(close=lambda: None)

    def start(self, foreground: bool = False, *, note: bool = True) -> None:
        return None

    def request(self, method: str, path: str, body: Any = None) -> Any:
        if method == "POST" and path == "/downloads":
            return dict(self.polls[0], state="queued")
        assert method == "GET" and path == "/downloads"
        if len(self.polls) > 1:
            return {"items": [self.polls.pop(0)]}
        return {"items": [self.polls[0]]}


def _item(state: str, progress: float | None, done: int, total: int) -> dict[str, Any]:
    return {
        "id": "org/repo",
        "state": state,
        "progress": progress,
        "bytes_done": done,
        "bytes_total": total,
        "speed_bps": None,
        "eta_s": None,
    }


def _pull(monkeypatch: pytest.MonkeyPatch, stderr: io.StringIO, polls: list[dict[str, Any]]) -> int:
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(cli_module, "Client", lambda port=None: _FakeClient(polls))
    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: None)
    return cli_module.main(["pull", "org/repo"])


def test_a_tty_pull_draws_the_overall_bar_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("NO_COLOR", "1")
    stderr = _Tty()
    polls = [
        _item("downloading", 0.58, 580, 1000),
        _item("done", 1.0, 1000, 1000),
    ]
    assert _pull(monkeypatch, stderr, polls) == 0
    drawn = stderr.getvalue()
    # 58% of 20 cells: 12 filled, 8 empty; the line is rewritten with \r and erased first.
    assert "org/repo: " + "█" * 12 + "░" * 8 + " downloading 58%" in drawn
    assert "\r\033[K" in drawn


def test_a_non_tty_pull_prints_lines_without_a_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    stderr = io.StringIO()
    polls = [_item("downloading", 0.58, 580, 1000), _item("done", 1.0, 1000, 1000)]
    assert _pull(monkeypatch, stderr, polls) == 0
    text = stderr.getvalue()
    assert "pull org/repo: downloading 58%" in text
    assert "█" not in text and "░" not in text
