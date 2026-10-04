"""Quoting of the scripts `MacOS.open_in_terminal` hands to osascript (SPEC §10.7)."""

from __future__ import annotations

import re
import shlex

import pytest

from splash_gui.system.macos import CommandResult, MacOS

ARGS = ["/Users/me/.splash/bin/splash", "launch", "claude", "--model", 'o/r:p"; touch /tmp/x; "']


class Capture(MacOS):
    def __init__(self, terminal: str) -> None:
        self.terminal = terminal
        self.scripts: list[str] = []

    def default_terminal(self) -> str:
        return self.terminal

    def osascript(self, script: str) -> CommandResult:
        self.scripts.append(script)
        return CommandResult(0)


def literal(script: str) -> str:
    """The AppleScript string literal after `command` / `do script`, decoded."""
    match = re.search(r'(?:command|do script) "((?:[^"\\]|\\.)*)"', script)
    assert match, script
    return re.sub(r"\\(.)", r"\1", match.group(1))


@pytest.mark.parametrize("terminal", ["Terminal", "iTerm"])
def test_open_in_terminal_keeps_every_argument_intact(terminal: str) -> None:
    mac = Capture(terminal)
    mac.open_in_terminal(shlex.join(ARGS))
    text = literal(mac.scripts[0])
    if terminal == "iTerm":
        # Formerly `/bin/zsh -lc "<command>"`: a `"` in the model ended the -lc argument.
        zsh = shlex.split(text)
        assert zsh[:2] == ["/bin/zsh", "-lc"] and len(zsh) == 3
        text = zsh[2].removesuffix("; exec /bin/zsh -l")
    assert shlex.split(text) == ARGS
