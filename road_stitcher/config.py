from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


WORK_DIR_NAME = "处理数据"


@dataclass(slots=True)
class JobConfig:
    video_path: Path
    output_dir: Path
    project_name: str = "航拍道路拼接"
    stitch_mode: str = "road"
    interval_mode: str = "auto"
    interval_seconds: float = 2.0
    road_left_ratio: float = 0.24
    road_right_ratio: float = 0.76
    make_preview: bool = True
    make_high_resolution: bool = True
    preview_frame_width: int = 600
    high_frame_width: int = 1920
    preview_max_canvas_pixels: int = 45_000_000
    high_max_canvas_pixels: int = 110_000_000
    jpeg_quality: int = 94

    def validate(self) -> None:
        self.video_path = Path(self.video_path).expanduser().resolve()
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        if not self.video_path.is_file():
            raise ValueError(f"视频文件不存在：{self.video_path}")
        if self.video_path.suffix.lower() not in {
            ".mp4",
            ".mov",
            ".m4v",
            ".avi",
            ".mkv",
        }:
            raise ValueError("暂不支持该视频格式")
        if self.interval_mode not in {"auto", "manual"}:
            raise ValueError("抽帧模式必须为 auto 或 manual")
        if self.stitch_mode not in {"road", "free"}:
            raise ValueError("拼接模式必须为 road 或 free")
        if not 0.25 <= self.interval_seconds <= 30.0:
            raise ValueError("抽帧间隔必须在0.25至30秒之间")
        if not 0.0 <= self.road_left_ratio < self.road_right_ratio <= 1.0:
            raise ValueError("道路区域比例设置无效")
        if self.road_right_ratio - self.road_left_ratio < 0.2:
            raise ValueError("道路分析区域过窄")
        if not 320 <= self.preview_frame_width <= 2000:
            raise ValueError("预览单帧宽度设置无效")
        if not 600 <= self.high_frame_width <= 3840:
            raise ValueError("高清单帧宽度设置无效")
        if not 80 <= self.jpeg_quality <= 100:
            raise ValueError("JPEG质量必须在80至100之间")

    @property
    def job_file(self) -> Path:
        return self.work_dir / "job.json"

    @property
    def log_file(self) -> Path:
        return self.work_dir / "job.log"

    @property
    def work_dir(self) -> Path:
        """Keep caches and diagnostics away from user-facing result images."""
        return self.output_dir / WORK_DIR_NAME

    def frames_dir(self, interval_seconds: float) -> Path:
        label = f"{interval_seconds:g}".replace(".", "_")
        return self.work_dir / f"frames_{label}s"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["video_path"] = str(self.video_path)
        result["output_dir"] = str(self.output_dir)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobConfig":
        values = dict(data)
        values["video_path"] = Path(values["video_path"])
        values["output_dir"] = Path(values["output_dir"])
        return cls(**values)
