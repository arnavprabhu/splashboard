"""Installing the `splash` CLI shim (SPEC §12.1, D21).

The shim is a small shell script in `~/.splash/bin` that runs this package's
entry point, so our commands come before Homebrew's `splash` on PATH and
everything else is exec'd through to the real engine. Two separate jobs:

  * the script itself, which we own and can rewrite or remove;
  * the `PATH` line in the user's shell rc files, which we only ever touch
    inside a marked block so uninstalling removes exactly what we added.

Nothing here runs a shell to edit an rc file: the block is appended and
written back atomically. Removal gives back the file's exact bytes: the block
records whether it created the file or had to end the last line, the file is
handled as bytes (any encoding, CRLF kept), a symlinked rc file is edited
through its link, and the file's mode is kept.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

from ..engine.discovery import SHIM_MARKER
from ..paths import Paths, write_atomic

BEGIN = "# >>> splash gui (managed block, do not edit) >>>"
END = "# <<< splash gui <<<"
# Lines inside the block that let removal restore the exact original bytes.
CREATED = "# splash gui: this file did not exist before"
NEWLINE_ADDED = "# splash gui: ended the line above with a newline"
RC_FILES = (".zprofile", ".bash_profile")
RC_MODE = 0o644
SHIM_MODE = 0o700


def interpreter_command(project: Path | None = None) -> list[str]:
    """The argv that runs this package's `splash` entry point.

    The source tree is the installed form for now (D30), so this is the same
    `uv run --project <repo>/manager` the menu bar app uses. Once packaging
    ships, the bundled Python takes this place.
    """
    if project is None:
        # manager/splash_gui/cli/install.py -> manager/
        project = Path(__file__).resolve().parents[2]
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    return [uv, "run", "--project", str(project), "splash"]


def render(launch: list[str]) -> bytes:
    """The shim script. POSIX sh, quoted arguments, exec so signals reach us."""
    quoted = " ".join(_shell_quote(part) for part in launch)
    return (
        "#!/bin/sh\n"
        "# splash - Splash GUI CLI (SPEC §12.1). Generated; edits are overwritten.\n"
        "# Our commands are handled here; anything else is exec'd to the real\n"
        "# Splash engine, found by skipping this script on PATH.\n"
        f"# {SHIM_MARKER}: engine discovery skips any file carrying this marker.\n"
        f'exec {quoted} "$@"\n'
    ).encode()


def _shell_quote(value: str) -> str:
    if value and all(c.isalnum() or c in "-_./=:" for c in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def install_shim(paths: Paths, launch: list[str] | None = None) -> Path:
    """Write `~/.splash/bin/splash`, executable by its owner only."""
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    paths.bin_dir.chmod(0o700)
    write_atomic(paths.shim, render(launch or interpreter_command()), mode=SHIM_MODE)
    paths.shim.chmod(SHIM_MODE)
    return paths.shim


def remove_shim(paths: Paths) -> bool:
    try:
        paths.shim.unlink()
    except FileNotFoundError:
        return False
    return True


def has_block(text: str) -> bool:
    return _find_block(text) is not None


def _find_block(text: str) -> tuple[int, int, str] | None:
    """(start, stop, block) of the first marked block, BEGIN at a line start."""
    start = text.find(BEGIN)
    while start > 0 and text[start - 1] != "\n":
        start = text.find(BEGIN, start + 1)
    if start < 0:
        return None
    end = text.find(END, start)
    if end < 0:
        return None
    stop = end + len(END)
    if text.startswith("\n", stop):
        stop += 1
    return start, stop, text[start:stop]


def strip_block(text: str) -> str:
    """The text with our marked block(s) and the separator we added removed:
    for a file `add_to_rc` changed, exactly the original text."""
    while (found := _find_block(text)) is not None:
        start, stop, block = found
        head, tail = text[:start], text[stop:]
        if head.endswith("\n\n"):
            # The blank line before the block is ours; with NEWLINE_ADDED, so is
            # the newline that ended the user's last line.
            head = head[:-2] if f"\n{NEWLINE_ADDED}\n" in block else head[:-1]
        text = head + tail
    return text


def path_block(*flags: str) -> str:
    lines = [BEGIN, *flags, 'export PATH="$HOME/.splash/bin:$PATH"', END]
    return "\n".join(lines) + "\n"


def _rc_target(path: Path) -> Path:
    """Edit a symlinked rc file (dotfile managers link them) through its link,
    so the link survives; a broken link is left alone."""
    if not path.is_symlink():
        return path
    target = path.resolve()
    if not target.exists():
        raise OSError(f"{path} is a broken symlink; fix it or add the PATH line yourself")
    return target


def _read_rc(path: Path) -> str | None:
    """The file as text that round-trips to the same bytes (any encoding)."""
    try:
        return path.read_bytes().decode("utf-8", "surrogateescape")
    except (FileNotFoundError, NotADirectoryError):
        return None


def _write_rc(path: Path, text: str, existed: bool) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if existed else RC_MODE
    write_atomic(path, text.encode("utf-8", "surrogateescape"), mode=mode)


def add_to_rc(path: Path, bin_dir: Path) -> bool:
    """Put the PATH block in `path`. Returns whether the file changed. Only ever
    called on the user's explicit request (SPEC §12.1)."""
    target = _rc_target(path)
    original = _read_rc(target)
    text = original or ""
    if has_block(text):
        return False
    if original is None:
        new = path_block(CREATED)
    elif not text:
        new = path_block()
    elif text.endswith("\n"):
        new = text + "\n" + path_block()
    else:
        new = text + "\n\n" + path_block(NEWLINE_ADDED)
    _write_rc(target, new, existed=original is not None)
    return True


def remove_from_rc(path: Path) -> bool:
    try:
        target = _rc_target(path)
    except OSError:
        return False
    text = _read_rc(target)
    if text is None:
        return False
    found = _find_block(text)
    if found is None:
        return False
    stripped = strip_block(text)
    if not stripped and f"\n{CREATED}\n" in found[2]:
        target.unlink()  # we created it and nothing else was added since
        return True
    _write_rc(target, stripped, existed=True)
    return True


def rc_report(
    home: Path | None = None, names: tuple[str, ...] = RC_FILES
) -> list[dict[str, object]]:
    """Which rc files already carry our block, for the settings page."""
    home = home or Path.home()
    report: list[dict[str, object]] = []
    for name in names:
        path = home / name
        try:
            text = _read_rc(path) or ""
        except OSError:
            text = ""
        report.append({"file": str(path), "present": path.exists(), "managed": has_block(text)})
    return report


def on_path(bin_dir: Path | None = None) -> bool:
    """Whether our bin directory already precedes the rest of PATH."""
    entries = (bin_dir or Path.home() / ".splash" / "bin").as_posix()
    seen: list[str] = []
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        resolved = Path(entry).expanduser().as_posix()
        if resolved not in seen:
            seen.append(resolved)
    return bool(seen) and seen[0] == entries


def shim_is_ours(paths: Paths) -> bool:
    """Whether `~/.splash/bin/splash` is the script we generated."""
    try:
        return paths.shim.read_text().startswith("#!/bin/sh\n# splash - Splash GUI CLI")
    except (FileNotFoundError, UnicodeDecodeError):
        return False


def self_test(paths: Paths, home: Path | None = None) -> list[str]:
    """Problems worth showing the user, in the order they should fix them."""
    problems: list[str] = []
    if not paths.shim.exists():
        problems.append(f"{paths.shim} is not installed")
    elif not stat.S_IMODE(paths.shim.stat().st_mode) & stat.S_IXUSR:
        problems.append(f"{paths.shim} is not executable")
    if not on_path(paths.bin_dir):
        problems.append(f"{paths.bin_dir} is not first on PATH")
    for entry in rc_report(home):
        if not entry["managed"]:
            problems.append(f"{entry['file']} has no Splash GUI PATH block")
    return problems


def current_python() -> str:
    return sys.executable
