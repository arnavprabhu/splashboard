"""`splash doctor` (docs/ui/11 §12, SPEC §12.1–12.2).

Local checks always run, so the command works when nothing else does; when the
manager answers, its `POST /doctor` items are merged in. A local check replaces
the manager's item with the same id: the CLI is the one that can see the
user's shells, and it applies the §12 thresholds itself.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import Paths
from .output import Style, fmt_bytes, home_relative, plural

GIB = 1024**3
DISK_WARN_BYTES = 10 * GIB  # §12: < 10 GB free is `!`
DISK_FAIL_BYTES = 2 * GIB  # §12: < 2 GB free is `✗`
SHELL_TIMEOUT_S = 3.0

GROUPS = (
    ("hardware", "Hardware & OS"),
    ("engine", "Engine"),
    ("shell", "Shell"),
    ("manager", "Manager"),
    ("storage", "Storage"),
    ("hf", "Hugging Face"),
    ("integrations", "Integrations"),
    ("other", "Other"),
)
# The manager's `POST /doctor` ids → §12 groups.
GROUP_OF = {
    "hardware": "hardware",
    "engine": "engine",
    "brew": "engine",
    "path": "shell",
    "shim": "shell",
    "manager": "manager",
    "ports": "manager",
    "disk": "storage",
    "permissions": "storage",
    "hf_token": "hf",
    "integrations": "integrations",
}

Runner = Callable[[list[str]], str | None]


@dataclass
class Check:
    id: str
    status: str  # ok | warn | fail
    title: str
    detail: list[str] = field(default_factory=list)
    fix: list[str] = field(default_factory=list)
    group: str = ""

    def __post_init__(self) -> None:
        self.group = self.group or GROUP_OF.get(self.id, "other")

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group,
            "status": self.status,
            "title": self.title,
            "detail": "\n".join(self.detail) or None,
            "fix": list(self.fix),
        }


def run_shell(argv: list[str]) -> str | None:
    """Run an interactive shell's `type -a splash` with a short timeout."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout


_DEFINITION = re.compile(r"^\s*(?:function\s+splash\b|splash\s*\(\s*\)|alias\s+splash=)")
_BREW_SHELLENV = re.compile(r"\bbrew\s+shellenv\b")
# zsh says where a function came from: "splash is a shell function from /Users/x/.zshrc".
_FROM = re.compile(r" from (/.+)$")
RC_FILES = (".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".profile")


@dataclass(frozen=True)
class Site:
    """The line that defines `splash()` or an alias for it (SPEC §12.1)."""

    path: Path
    line: int
    kind: str  # "function" or "alias"

    @property
    def where(self) -> str:
        return f"{home_relative(self.path)}:{self.line}"


def first_match(path: Path, pattern: re.Pattern[str]) -> tuple[int, str] | None:
    """The 1-based line number and text of the first line of `path` that matches."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for number, text in enumerate(lines, 1):
        if pattern.search(text):
            return number, text
    return None


def _first_in(paths: Iterable[Path], pattern: re.Pattern[str]) -> tuple[Path, int, str] | None:
    for path in paths:
        found = first_match(path, pattern) if path.is_file() else None
        if found:
            return path, found[0], found[1]
    return None


def definition_site(home: Path, reported: str | None = None) -> Site | None:
    """Where the function or alias is written: the file zsh names, then the startup files."""
    paths = ([Path(reported)] if reported else []) + [home / name for name in RC_FILES]
    found = _first_in(paths, _DEFINITION)
    if found is None:
        return None
    path, line, text = found
    kind = "alias" if text.lstrip().startswith("alias") else "function"
    return Site(path, line, kind)


def shell_check(paths: Paths, home: Path, runner: Runner = run_shell) -> Check:
    """SPEC §12.1: a function, an alias or an earlier PATH hit hides the shim."""
    shim = str(paths.shim)
    shim_text = home_relative(shim)
    hidden_by: list[str] = []
    reported: str | None = None
    earlier: list[str] = []
    for shell in ("zsh", "bash"):
        if not shutil.which(shell) and runner is run_shell:
            continue
        # A login interactive shell is what a new Terminal tab runs: it reads ~/.zprofile too.
        output = runner([shell, "-lic", "type -a splash"]) or ""
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if not lines:
            continue
        first = lines[0]
        if "function" in first or "alias" in first:
            hidden_by.append(shell)
            source = _FROM.search(first)
            if source and reported is None:
                reported = source.group(1)
        elif any(shim in line for line in lines) and shim not in first:
            earlier.append(first)
    if hidden_by:
        reload = "exec zsh" if "zsh" in hidden_by else "exec bash"
        check_line = f"Check: type -a splash   → the first line should be {shim_text}"
        site = definition_site(home, reported)
        if site is None:
            return Check(
                "path",
                "fail",
                "`splash` is hidden by a shell function",
                detail=[
                    "your shell startup files define splash() or an alias, so your shell runs "
                    f"it instead of {shim_text}."
                ],
                fix=[
                    "rename splash() to splash-legacy in your shell startup files, "
                    f"then run: {reload}",
                    check_line,
                ],
            )
        if site.kind == "alias":
            return Check(
                "path",
                "fail",
                "`splash` is hidden by an alias",
                detail=[
                    f"{site.where} defines an alias for splash, so your shell runs it "
                    f"instead of {shim_text}."
                ],
                fix=[f"remove the alias at {site.where}, then run: {reload}", check_line],
            )
        return Check(
            "path",
            "fail",
            "`splash` is hidden by a shell function",
            detail=[
                f"{site.where} defines splash(), so your shell runs it instead of {shim_text}."
            ],
            fix=[
                f"rename splash() at {site.where} to splash-legacy, then run: {reload}",
                check_line,
            ],
        )
    if earlier:
        found = _first_in([home / name for name in RC_FILES], _BREW_SHELLENV)
        fix = [f"put {home_relative(paths.bin_dir)} first on PATH, then open a new terminal"]
        if found:
            path, line, _ = found
            fix = [
                f"the `brew shellenv` at {home_relative(path)}:{line} puts Homebrew ahead of "
                f"{shim_text}. Remove that line, or keep it above every Splashboard PATH block, "
                "then open a new terminal"
            ]
        return Check(
            "path",
            "fail",
            "`splash` runs another program first",
            detail=[f"{earlier[0]} comes before {shim_text} on PATH."],
            fix=fix,
        )
    return Check("path", "ok", "No shell function or alias hides `splash`")


def disk_check(models_dir: Path, models_bytes: int | None = None) -> Check:
    probe = models_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as error:
        return Check(
            "disk",
            "warn",
            f"Models {home_relative(models_dir)} · free space unknown",
            detail=[str(error)],
        )
    size = f" · {fmt_bytes(models_bytes)}" if models_bytes is not None else ""
    title = f"Models {home_relative(models_dir)}{size} · {fmt_bytes(free)} free"
    if free < DISK_FAIL_BYTES:
        return Check(
            "disk",
            "fail",
            title,
            detail=["Downloads and the SSD cache need room."],
            fix=["free some disk space, or move the models: splash open settings"],
        )
    if free < DISK_WARN_BYTES:
        return Check(
            "disk",
            "warn",
            title,
            detail=["Less than 10 GB free: a model may not fit."],
            fix=["free some disk space, or move the models: splash open settings"],
        )
    return Check("disk", "ok", title)


def _mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def permissions_check(paths: Paths) -> Check:
    entries = [(paths.base, 0o700), (paths.settings_file, 0o600), (paths.cli_token, 0o600)]
    shown, loose = [], []
    for path, wanted in entries:
        mode = _mode(path)
        if mode is None:
            continue
        label = home_relative(path) if path == paths.base else str(path.relative_to(paths.base))
        shown.append(f"{label} ({mode:04o})")
        if mode & 0o077:
            loose.append((path, wanted))
    title = " · ".join(shown) or home_relative(paths.base)
    if not loose:
        return Check("permissions", "ok", title)
    return Check(
        "permissions",
        "warn",
        title,
        detail=["Other users on this Mac can read Splashboard's private files."],
        fix=[" && ".join(f"chmod {wanted:o} {home_relative(p)}" for p, wanted in loose)],
    )


def engine_check(info: Any) -> Check:
    """Local engine discovery, for when the manager is not there to ask."""
    if not info.found:
        return Check(
            "engine",
            "fail",
            "Splash is not installed",
            detail=[str(info.error)] if info.error else [],
            fix=["brew install incoai/tap/splash"],
        )
    title = f"Splash {info.version} · {info.cli}   (Splashboard supports ≥ 1.3.0, < 1.4.0)"
    if info.support == "supported":
        return Check("engine", "ok", title)
    status = "fail" if info.support == "too_old" else "warn"
    return Check("engine", status, title, fix=["brew upgrade incoai/tap/splash"])


def from_manager(items: Iterable[dict[str, Any]]) -> list[Check]:
    checks = []
    for item in items:
        status = item.get("status", "ok")
        if status == "skip":
            continue
        label, message = item.get("label") or item.get("id"), item.get("message") or ""
        title = f"{label} · {message}" if message else str(label)
        fix = item.get("fix")
        checks.append(
            Check(
                str(item.get("id")),
                status,
                title,
                # An OK check never shows a fix (the manager may send one anyway).
                fix=[fix] if fix and status != "ok" else [],
            )
        )
    return checks


def merge(manager: list[Check], local: list[Check]) -> list[Check]:
    """Local checks replace the manager's item with the same id."""
    ids = {check.id for check in local}
    merged = [check for check in manager if check.id not in ids] + local
    groups = [group for group, _ in GROUPS]
    ids_order = list(GROUP_OF)

    def key(check: Check) -> tuple[int, int]:
        group = groups.index(check.group) if check.group in groups else len(groups)
        within = ids_order.index(check.id) if check.id in ids_order else len(ids_order)
        return group, within

    return sorted(merged, key=key)


def summary(checks: list[Check]) -> dict[str, int]:
    return {s: sum(1 for c in checks if c.status == s) for s in ("ok", "warn", "fail")}


def render(checks: list[Check], style: Style, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    lines = [style.bold(f"Splashboard doctor · {now:%Y-%m-%d %H:%M}")]
    titles = dict(GROUPS)
    for group, _ in GROUPS:
        members = [c for c in checks if c.group == group]
        if not members:
            continue
        lines += ["", style.bold(titles[group])]
        for check in members:
            lines.append(f"  {style.mark(check.status)} {check.title}")
            if check.status == "ok":
                continue
            lines += ["      " + line for line in check.detail]
            for index, fix in enumerate(check.fix):
                prefix = "" if fix.startswith("Check:") else "Fix: " if index == 0 else "     "
                lines.append("      " + prefix + fix)
    counts = summary(checks)
    lines += [
        "",
        " · ".join(
            [
                plural(counts["fail"], "problem"),
                plural(counts["warn"], "warning"),
                plural(counts["ok"], "check") + " passed",
            ]
        ),
    ]
    return lines


def home_dir() -> Path:
    return Path("~").expanduser()
