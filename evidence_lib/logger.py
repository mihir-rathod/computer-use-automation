"""Structured JSONL evidence log -- a record of what the agent did and why. One file per run.
Surface.act() is the single chokepoint that writes here (see surface/web.py), so evidence is a
byproduct of running the system, not something bolted on separately.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Self

from evidence_lib.redaction import Redactor


@dataclass
class EvidenceEvent:
    ts: float
    actor: str  # "agent" | "replay" | "human" | "system"
    event_type: str  # "perceive" | "action" | "checkpoint" | "error" | "pause" | "resume" | ...
    data: dict[str, Any] = field(default_factory=dict)


class EvidenceLogger:
    def __init__(self, run_dir: Path, redactor: Redactor | None = None):
        self.redactor = redactor
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "log.jsonl"
        self._fh = self.path.open("a", encoding="utf-8")

    def log(self, actor: str, event_type: str, **data: Any) -> None:
        if self.redactor is not None:
            data = self.redactor.scrub(data)
        event = EvidenceEvent(ts=time.time(), actor=actor, event_type=event_type, data=data)
        self._fh.write(json.dumps(asdict(event), default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
