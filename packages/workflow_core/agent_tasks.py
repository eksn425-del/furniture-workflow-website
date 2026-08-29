"""Durable host-Agent task and receipt contracts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


AGENT_TASK_SCHEMA_VERSION = "agent-task.v1"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class AgentTask:
    task_id: str
    order_id: str
    candidate_id: str
    task_type: str
    input_evidence: dict[str, Any]
    local_image_path: str | None
    required_output_schema: str
    status: str = "PENDING"
    receipt_path: str | None = None
    downstream_dirty: bool = False
    reconciled_at: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": AGENT_TASK_SCHEMA_VERSION, **asdict(self)}


class AgentTaskStore:
    """Atomic JSON queue; receipts are immutable inputs to the next stage."""

    def __init__(self, path: Path, *, order_id: str) -> None:
        self.path = Path(path)
        self.order_id = order_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": AGENT_TASK_SCHEMA_VERSION, "order_id": self.order_id, "tasks": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("order_id") != self.order_id:
            raise ValueError("Agent task order mismatch")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            temporary = Path(stream.name)
        os.replace(temporary, self.path)

    def create_or_get(
        self, *, candidate_id: str, task_type: str, input_evidence: dict[str, Any],
        local_image_path: str | None, required_output_schema: str,
    ) -> AgentTask:
        payload = self._read()
        task_id = f"{self.order_id}:{candidate_id}:{task_type}"
        existing = payload.setdefault("tasks", {}).get(task_id)
        if existing:
            return AgentTask(**{key: existing[key] for key in AgentTask.__dataclass_fields__ if key in existing})
        task = AgentTask(
            task_id=task_id,
            order_id=self.order_id,
            candidate_id=candidate_id,
            task_type=task_type,
            input_evidence=dict(input_evidence),
            local_image_path=local_image_path,
            required_output_schema=required_output_schema,
        )
        payload["tasks"][task_id] = task.to_dict()
        payload["updated_at"] = _now()
        self._write(payload)
        return task

    def set_receipt(self, task_id: str, *, receipt_path: str, status: str = "COMPLETED") -> AgentTask:
        payload = self._read()
        raw = payload.setdefault("tasks", {}).get(task_id)
        if not raw:
            raise KeyError(task_id)
        raw["receipt_path"] = receipt_path
        raw["status"] = status
        raw["downstream_dirty"] = True
        raw["reconciled_at"] = None
        raw["updated_at"] = _now()
        payload["updated_at"] = _now()
        self._write(payload)
        return AgentTask(**{key: raw[key] for key in AgentTask.__dataclass_fields__ if key in raw})

    def all(self) -> list[AgentTask]:
        """Return every durable task without changing task state."""

        payload = self._read()
        return [
            AgentTask(**{
                key: raw[key]
                for key in AgentTask.__dataclass_fields__
                if key in raw
            })
            for raw in payload.get("tasks", {}).values()
            if isinstance(raw, dict)
        ]

    def mark_downstream_reconciled(self, task_ids: list[str] | None = None) -> int:
        """Clear receipt dirtiness only after downstream derived views reconcile."""

        payload = self._read()
        selected = set(task_ids or payload.get("tasks", {}).keys())
        changed = 0
        now = _now()
        for task_id, raw in payload.get("tasks", {}).items():
            if task_id not in selected or not isinstance(raw, dict):
                continue
            if raw.get("status") != "COMPLETED" or not raw.get("downstream_dirty"):
                continue
            raw["downstream_dirty"] = False
            raw["reconciled_at"] = now
            raw["updated_at"] = now
            changed += 1
        if changed:
            payload["updated_at"] = now
            self._write(payload)
        return changed

    def get(self, task_id: str) -> AgentTask | None:
        """Return one durable task without changing its state."""

        raw = self._read().get("tasks", {}).get(task_id)
        if not raw:
            return None
        return AgentTask(**{
            key: raw[key]
            for key in AgentTask.__dataclass_fields__
            if key in raw
        })

    def pending(self) -> list[AgentTask]:
        payload = self._read()
        return [
            AgentTask(**{key: raw[key] for key in AgentTask.__dataclass_fields__ if key in raw})
            for raw in payload.get("tasks", {}).values()
            if str(raw.get("status") or "PENDING") in {"PENDING", "RUNNING"}
        ]


__all__ = ["AGENT_TASK_SCHEMA_VERSION", "AgentTask", "AgentTaskStore"]
