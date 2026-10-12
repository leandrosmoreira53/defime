"""Log estruturado, sem dependencia externa.

Formato JSON por linha para o indexer 24/7 (grepavel, parseavel), texto legivel
quando log_json=False para desenvolvimento.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "nivel": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, timezone.utc).strftime("%H:%M:%S")
        extra = getattr(record, "extra_fields", None) or {}
        tail = "  ".join(f"{k}={v}" for k, v in extra.items())
        return f"{stamp} {record.levelname:<7} {record.getMessage()}" + (f"  {tail}" if tail else "")


class StructuredLogger:
    """Wrapper fino: log.info("evento", chave=valor)."""

    def __init__(self, logger: logging.Logger):
        self._log = logger

    def _emit(self, level: int, msg: str, **fields):
        self._log.log(level, msg, extra={"extra_fields": fields})

    def debug(self, msg, **f):
        self._emit(logging.DEBUG, msg, **f)

    def info(self, msg, **f):
        self._emit(logging.INFO, msg, **f)

    def warning(self, msg, **f):
        self._emit(logging.WARNING, msg, **f)

    def error(self, msg, **f):
        self._emit(logging.ERROR, msg, **f)

    def exception(self, msg, **f):
        self._log.exception(msg, extra={"extra_fields": f})


_configured = False


def _configure():
    global _configured
    if _configured:
        return
    from .config import settings
    cfg = settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if cfg.log_json else _TextFormatter())
    root = logging.getLogger("defime")
    root.setLevel(cfg.log_level.upper())
    root.handlers = [handler]
    root.propagate = False
    _configured = True


def get_logger(name: str) -> StructuredLogger:
    _configure()
    short = name.replace("defime.", "")
    return StructuredLogger(logging.getLogger(f"defime.{short}"))


__all__ = ["get_logger", "StructuredLogger"]
