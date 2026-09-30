from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from .config import JobConfig


class JobStage(StrEnum):
    CREATED = "created"
    INSPECTING = "inspecting"
    RECOMMENDING_INTERVAL = "recommending_interval"
    EXTRACTING = "extracting"
    ANALYZING_OVERLAP = "analyzing_overlap"
    ALIGNING = "aligning"
    RENDERING_PREVIEW = "rendering_preview"
    RENDERING_HIGH = "rendering_high"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class JobState:
    config: dict[str, Any]
    stage: str = JobStage.CREATED
    progress: float = 0.0
    message: str = ""
    interval_seconds: float | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, config: JobConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self.state = self._load_or_create()

    def _load_or_create(self) -> JobState:
        if self.config.job_file.is_file():
            data = json.loads(self.config.job_file.read_text(encoding="utf-8"))
            return JobState(**data)
        return JobState(config=self.config.to_dict())

    def update(
        self,
        stage: JobStage | str | None = None,
        progress: float | None = None,
        message: str | None = None,
        **changes: Any,
    ) -> JobState:
        with self._lock:
            if stage is not None:
                self.state.stage = str(stage)
            if progress is not None:
                self.state.progress = max(0.0, min(1.0, float(progress)))
            if message is not None:
                self.state.message = message
            for key, value in changes.items():
                setattr(self.state, key, value)
            self.state.touch()
            self.save()
            return self.state

    def save(self) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.config.job_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for attempt in range(6):
            try:
                os.replace(temporary, self.config.job_file)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                # Windows may briefly lock the destination while Explorer,
                # antivirus software, or a status monitor is reading it.
                time.sleep(0.05 * (attempt + 1))

    def add_output(self, name: str, path: Path) -> None:
        outputs = dict(self.state.outputs)
        outputs[name] = str(path)
        self.update(outputs=outputs)


class PipelineControl:
    def __init__(self) -> None:
        self.pause_event = threading.Event()
        self.cancel_event = threading.Event()

    def pause(self) -> None:
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.pause_event.clear()

    def checkpoint(self) -> None:
        if self.cancel_event.is_set():
            raise PipelineCancelled("任务已取消")
        while self.pause_event.is_set():
            if self.cancel_event.is_set():
                raise PipelineCancelled("任务已取消")
            time.sleep(0.2)


class PipelineCancelled(RuntimeError):
    pass
