"""Small structured logging helpers with no external dependencies."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Mapping, Optional


class JsonFormatter(logging.Formatter):
    """Render daemon records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {})
        if not isinstance(fields, Mapping):
            fields = {}
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event_name", record.getMessage()),
            **dict(fields),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def configure_logging(level: str = "INFO", stream: Optional[Any] = None) -> logging.Logger:
    logger = logging.getLogger("noema.runtime")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
    if not logger.handlers:
        import os
        from logging.handlers import RotatingFileHandler

        # Add stream handler
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

        # Add file handler in logs directory
        try:
            log_dir = os.path.join(os.getcwd(), "logs")
            os.makedirs(log_dir, exist_ok=True)
            file_handler = RotatingFileHandler(
                os.path.join(log_dir, "daemon.log"),
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8"
            )
            file_handler.setFormatter(JsonFormatter())
            logger.addHandler(file_handler)
        except Exception:
            pass
    else:
        for h in logger.handlers:
            h.setLevel(logger.level)
    return logger


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    logger.log(level, event, extra={"event_name": event, "fields": fields})
