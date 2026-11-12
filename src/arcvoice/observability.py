"""JSON logs with a request id, plus a tiny Prometheus text endpoint.

Kept dependency free on purpose. On GCP, Cloud Logging parses these JSON
lines and the `severity` field directly.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from collections import Counter
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": request_id.get(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def log(logger: logging.Logger, message: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, message, extra={"fields": fields})


class Metrics:
    def __init__(self) -> None:
        self._counts: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self._lock = threading.Lock()

    def inc(self, name: str, **labels: str) -> None:
        with self._lock:
            self._counts[(name, tuple(sorted(labels.items())))] += 1

    def value(self, name: str, **labels: str) -> int:
        return self._counts[(name, tuple(sorted(labels.items())))]

    def render(self) -> str:
        lines = []
        with self._lock:
            for (name, labels), count in sorted(self._counts.items()):
                label_str = ",".join(f'{k}="{v}"' for k, v in labels)
                lines.append(f"{name}{{{label_str}}} {count}" if label_str else f"{name} {count}")
        return "\n".join(lines) + "\n"
