from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def format_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class BatchProgress:
    """Estimate the current stage from work performed in this invocation."""

    def __init__(self, total: int, cached: int = 0):
        self.total = total
        self.cached = cached
        self.completed = cached
        self.started = time.monotonic()

    def suffix(self, *, advance: bool = False) -> str:
        if advance:
            self.completed += 1
        elapsed = time.monotonic() - self.started
        remaining = self.total - self.completed
        processed = self.completed - self.cached
        percent = 100 * self.completed / self.total if self.total else 100
        message = f"{percent:.1f}% processed | stage elapsed {format_duration(elapsed)}"
        if remaining == 0:
            return message + " | stage finished"
        if not processed:
            return message + " | stage ETA estimating after first batch"
        eta = elapsed / processed * remaining
        finish = datetime.now().astimezone() + timedelta(seconds=eta)
        return (
            message + f" | ~{format_duration(eta)} remaining"
            f" | estimated stage finish {finish:%Y-%m-%d %H:%M:%S %Z}"
        )


class EventLogger:
    def __init__(self, run_id: str, output_dir: str | Path):
        self.run_id = run_id
        self.started = time.monotonic()
        log_dir = Path(output_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"{run_id}.jsonl"
        self._stream = self.path.open("a", encoding="utf-8")
        self._console = logging.getLogger(f"eso_object_types.{run_id}")
        self._console.setLevel(logging.INFO)
        self._console.propagate = False
        if not self._console.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            self._console.addHandler(handler)

    def close(self) -> None:
        self._stream.close()

    def event(self, level: str, event: str, message: str, **fields: Any) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level.upper(),
            "run_id": self.run_id,
            "event": event,
            "message": message,
            **fields,
        }
        self._stream.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        self._stream.flush()
        log_method = getattr(self._console, level.lower(), self._console.info)
        elapsed = format_duration(time.monotonic() - self.started)
        log_method(f"{message} | run elapsed {elapsed}")

    def info(self, event: str, message: str, **fields: Any) -> None:
        self.event("INFO", event, message, **fields)

    def warning(self, event: str, message: str, **fields: Any) -> None:
        self.event("WARNING", event, message, **fields)

    def error(self, event: str, message: str, **fields: Any) -> None:
        self.event("ERROR", event, message, **fields)
