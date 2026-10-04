"""Installing the `splash` CLI shim (SPEC §12.1, D21).

The shim is a small shell script in `~/.splash/bin` that runs this package's
entry point, so our commands come before Homebrew's `splash` on PATH and
everything else is exec'd through to the real engine. Two separate jobs:

  * the script itself, which we own and can rewrite or remove;
  * the `PATH` line in the user's shell rc files, which we only ever touch
    inside a marked block so uninstalling removes exactly what we added.

Nothing here runs a shell to edit an rc file: the block is read, inserted after
the existing preamble, and written back atomically.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

from ..paths import Paths, write_atomic

BEGIN = "# >>> splash gui (managed block, do not edit) >>>"
END = "# <<< splash gui <<<"
RC_FILES = (".zprofile", ".bash_profile")
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
    return BEGIN in text and END in text


def strip_block(text: str) -> str:
    """Remove our marked block and the blank line it leaves behind."""
    if not has_block(text):
        return text
    head, _, rest = text.partition(BEGIN)
    _, _, tail = rest.partition(END)
    return (head.rstrip("\n") + "\n" + tail.lstrip("\n")).lstrip("\n") or "\n"


def path_block() -> str:
    return f'{BEGIN}\nexport PATH="$HOME/.splash/bin:$PATH"\n{END}\n'


def add_to_rc(path: Path, bin_dir: Path) -> bool:
    """Put the PATH block in `path`. Returns whether the file changed."""
    try:
        text = path.read_text()
    except (FileNotFoundError, NotADirectoryError, UnicodeDecodeError):
        text = ""
    if BEGIN in text and END in text:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    if text.strip():
        text += "\n"
    write_atomic(path, (text + path_block()).encode(), mode=0o644)
    return True


def remove_from_rc(path: Path) -> bool:
    try:
        text = path.read_text()
    except (FileNotFoundError, NotADirectoryError, UnicodeDecodeError):
        return False
    if not has_block(text):
        return False
    write_atomic(path, strip_block(text).encode(), mode=0o644)
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
            text = path.read_text()
        except (FileNotFoundError, NotADirectoryError, UnicodeDecodeError):
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


def self_test(paths: Paths) -> list[str]:
    """Problems worth showing the user, in the order they should fix them."""
    problems: list[str] = []
    if not paths.shim.exists():
        problems.append(f"{paths.shim} is not installed")
    elif not stat.S_IMODE(paths.shim.stat().st_mode) & stat.S_IXUSR:
        problems.append(f"{paths.shim} is not executable")
    if not on_path(paths.bin_dir):
        problems.append(f"{paths.bin_dir} is not first on PATH")
    for entry in rc_report():
        if not entry["managed"]:
            problems.append(f"{entry['file']} has no Splash GUI PATH block")
    return problems


def current_python() -> str:
    return sys.executable
