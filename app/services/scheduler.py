import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo


class ScheduledRunStore(Protocol):
    def is_complete(self, schedule_name: str, run_id: str) -> bool:
        ...

    def mark_complete(self, schedule_name: str, run_id: str) -> None:
        ...


class JsonScheduledRunStore:
    """Small durable ledger for completed schedule windows."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()

    def is_complete(self, schedule_name: str, run_id: str) -> bool:
        with self._lock:
            return self._load().get(schedule_name) == run_id

    def mark_complete(self, schedule_name: str, run_id: str) -> None:
        with self._lock:
            state = self._load()
            state[schedule_name] = run_id
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temporary = f"{self.path}.tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, self.path)

    def _load(self) -> dict[str, str]:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in payload.items()
        ):
            raise ValueError("Scheduled run ledger must be a string mapping")
        return payload


class ScheduledInputProvider(Protocol):
    def build_input(
        self,
        schedule_name: str,
        run_id: str,
        phase_config: dict,
    ) -> bytes:
        ...


def latest_daily_run_id(
    schedule_name: str,
    daily_at: str,
    timezone_name: str,
    now: datetime | None = None,
) -> str:
    """Return the latest due daily window as a deterministic identifier."""
    hour, minute = (int(part) for part in daily_at.split(":"))
    timezone = ZoneInfo(timezone_name)
    current = now or datetime.now(timezone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone)
    else:
        current = current.astimezone(timezone)
    scheduled_today = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    due = scheduled_today if current >= scheduled_today else scheduled_today - timedelta(days=1)
    return f"{schedule_name}:{due.date().isoformat()}"


def scheduled_workspace(workspace_root: str, schedule_name: str, run_id: str) -> str:
    safe_run_id = run_id.replace(":", "-")
    path = Path(workspace_root).resolve() / "scheduled" / schedule_name / safe_run_id
    path.mkdir(parents=True, exist_ok=True)
    return str(path)
