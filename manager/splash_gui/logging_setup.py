"""Rotating logs (SPEC §5): manager.log and engine.log, 10 MB × 5, with secret redaction."""

from __future__ import annotations

import contextlib
import io
import logging
import logging.handlers
from collections.abc import Callable, Iterable
from pathlib import Path

from .paths import FILE_MODE, Paths, ensure_private_dir
from .secrets import redact_text

MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
MANAGER_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
ENGINE_FORMAT = "%(asctime)s %(stream)s %(message)s"
ENGINE_LOGGER = "splash_gui.engine.output"


class RedactingFilter(logging.Filter):
    """Scrubs secrets from the formatted message before any handler writes it."""

    def __init__(self, known: Callable[[], Iterable[str]] = tuple) -> None:
        super().__init__()
        self._known = known

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_text(message, self._known())
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


class PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A RotatingFileHandler whose files are created 0600."""

    def _open(self) -> io.TextIOWrapper:
        stream = super()._open()
        with contextlib.suppress(OSError):
            Path(self.baseFilename).chmod(FILE_MODE)
        return stream


def _handler(path: str, fmt: str, known: Callable[[], Iterable[str]]) -> logging.Handler:
    handler = PrivateRotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(RedactingFilter(known))
    return handler


def setup_logging(
    paths: Paths,
    *,
    level: int = logging.INFO,
    known_secrets: Callable[[], Iterable[str]] = tuple,
    console: bool = False,
) -> None:
    """Configure the `splash_gui` logger tree and the engine output logger. Idempotent."""
    ensure_private_dir(paths.logs_dir)
    root = logging.getLogger("splash_gui")
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.addHandler(_handler(str(paths.manager_log), MANAGER_FORMAT, known_secrets))
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(MANAGER_FORMAT))
        stream.addFilter(RedactingFilter(known_secrets))
        root.addHandler(stream)

    engine = logging.getLogger(ENGINE_LOGGER)
    engine.propagate = False
    engine.setLevel(logging.INFO)
    for handler in list(engine.handlers):
        engine.removeHandler(handler)
        handler.close()
    engine.addHandler(_handler(str(paths.engine_log), ENGINE_FORMAT, known_secrets))


def engine_output_logger() -> logging.LoggerAdapter[logging.Logger]:
    """Where the supervisor writes engine stdout/stderr lines: `log.info(line, extra=...)`.

    Use `logger.info(line, extra={"stream": "stdout"|"stderr"})`.
    """
    return logging.LoggerAdapter(logging.getLogger(ENGINE_LOGGER), {"stream": "stdout"})


SESSION_START = "=== Splash GUI: engine session started"
SESSION_END = "=== Splash GUI: engine session ended"


class EngineLogWriter:
    """Writes engine stdout/stderr to engine.log (10 MB × 5, 0600, redacted).

    Owned by the supervisor rather than the global logging tree, so each manager
    (and each test) writes its own file.
    """

    def __init__(self, path: Path, known: Callable[[], Iterable[str]] = tuple) -> None:
        ensure_private_dir(path.parent)
        self._handler = PrivateRotatingFileHandler(
            str(path), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        self._handler.setFormatter(logging.Formatter(ENGINE_FORMAT))
        self._handler.addFilter(RedactingFilter(known))

    def write(self, text: str, stream: str = "stdout") -> None:
        record = logging.makeLogRecord(
            {
                "name": ENGINE_LOGGER,
                "levelno": logging.INFO,
                "levelname": "INFO",
                "msg": text,
                "args": None,
                "stream": stream,
            }
        )
        self._handler.handle(record)

    def close(self) -> None:
        self._handler.close()
