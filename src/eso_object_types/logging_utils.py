from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EventLogger:
    def __init__(self, run_id: str, output_dir: str | Path):
        self.run_id = run_id
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
        log_method(message)

    def info(self, event: str, message: str, **fields: Any) -> None:
        self.event("INFO", event, message, **fields)

    def warning(self, event: str, message: str, **fields: Any) -> None:
        self.event("WARNING", event, message, **fields)

    def error(self, event: str, message: str, **fields: Any) -> None:
        self.event("ERROR", event, message, **fields)

