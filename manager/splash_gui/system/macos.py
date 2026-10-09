"""Thin wrappers over the macOS commands the manager runs: `open`, `osascript`,
`pgrep`, `defaults`. One `MacOS` instance lives on the state (`state.macos`), so
tests replace it with a recorder and never drive real apps.
"""

from __future__ import annotations

import plistlib
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT_S = 15


def applescript_string(text: str) -> str:
    """`text` as an AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class MacOS:
    """Real implementations; `RecordingMacOS` in tests."""

    def run(self, argv: list[str], timeout: float = TIMEOUT_S) -> CommandResult:
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return CommandResult(127, "", str(error))
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def osascript(self, script: str) -> CommandResult:
        return self.run(["/usr/bin/osascript", "-e", script])

    def open(self, *args: str) -> CommandResult:
        return self.run(["/usr/bin/open", *args])

    def reveal(self, path: Path) -> CommandResult:
        """Reveal in Finder (`open -R`)."""
        return self.open("-R", str(path))

    def trash(self, path: Path) -> CommandResult:
        """Move a file to the Trash through Finder. Never a hard delete."""
        return self.osascript(
            f'tell application "Finder" to delete POSIX file {applescript_string(str(path))}'
        )

    def pgrep(self, *args: str) -> list[int]:
        result = self.run(["/usr/bin/pgrep", *args])
        return [int(x) for x in result.stdout.split() if x.isdigit()]

    def default_terminal(self) -> str:
        """`iTerm` when iTerm2 is the default handler for shell scripts, else `Terminal`."""
        result = self.run(
            [
                "/usr/bin/defaults",
                "read",
                "com.apple.LaunchServices/com.apple.launchservices.secure",
                "LSHandlers",
            ]
        )
        return "iTerm" if "com.googlecode.iterm2" in result.stdout else "Terminal"

    def open_in_terminal(self, command: str) -> CommandResult:
        """Run `command` in the user's default terminal ("Open in Terminal")."""
        if self.default_terminal() == "iTerm":
            # `command` is already shell-quoted; it has to be quoted once more as the
            # one argument of `zsh -lc`, or a double quote inside it (shlex.join
            # writes `'"'"'`) would end that argument early.
            inner = shlex.join(["/bin/zsh", "-lc", f"{command}; exec /bin/zsh -l"])
            script = (
                'tell application "iTerm" to create window with default profile command '
                f"{applescript_string(inner)}"
            )
        else:
            script = (
                f'tell application "Terminal"\n activate\n do script '
                f"{applescript_string(command)}\nend tell"
            )
        return self.osascript(script)

    def quit_app(self, name: str | None = None, bundle_id: str | None = None) -> CommandResult:
        target = f'id "{bundle_id}"' if bundle_id else f'"{name}"'
        return self.osascript(f"tell application {target} to quit")

    def app_version(self, app: Path) -> str | None:
        try:
            with (app / "Contents" / "Info.plist").open("rb") as handle:
                info = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        version = info.get("CFBundleShortVersionString") or info.get("CFBundleVersion")
        return str(version) if version else None


@dataclass
class RecordingMacOS(MacOS):
    """Records every command instead of running it (tests)."""

    calls: list[list[str]] = field(default_factory=list)
    running: dict[str, list[int]] = field(default_factory=dict)
    terminal: str = "Terminal"

    def run(self, argv: list[str], timeout: float = TIMEOUT_S) -> CommandResult:
        self.calls.append(list(argv))
        if argv and argv[0].endswith("pgrep"):
            pattern = argv[-1]
            pids = next((v for k, v in self.running.items() if k in pattern), [])
            return CommandResult(0 if pids else 1, " ".join(map(str, pids)))
        return CommandResult(0)

    def default_terminal(self) -> str:
        return self.terminal

    def commands(self) -> list[str]:
        return [shlex.join(c) for c in self.calls]
