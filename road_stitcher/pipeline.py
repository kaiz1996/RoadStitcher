from __future__ import annotations

import json
import re
import traceback
from pathlib import Path
from typing import Callable

from .config import JobConfig
from .extractor import extract_frames, extraction_is_complete
from .free_stitch import (
    align_free_translation,
    load_free_alignment,
    render_free_mosaic,
)
from .interval import recommend_interval
from .legacy_bridge import (
    load_precise_alignment,
    precise_alignment_is_current,
    render_curved_mosaic,
    run_overlap_analysis,
    run_precise_alignment,
)
from .logging_utils import create_job_logger
from .media import VideoInfo, inspect_video
from .state import (
    JobStage,
    JobStore,
    PipelineCancelled,
    PipelineControl,
)


ProgressCallback = Callable[[str, float, str], None]
LogCallback = Callable[[str], None]
ROAD_RENDERER_VERSION = "road-edge-feather-v2"
FREE_RENDERER_VERSION = "free-dp-seam-native-v6"


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return cleaned or "航拍道路拼接"


def road_render_signature(
    frame_width: int,
    max_canvas_pixels: int,
) -> dict[str, int | str]:
    return {
        "renderer": ROAD_RENDERER_VERSION,
        "frame_width": int(frame_width),
        "max_canvas_pixels": int(max_canvas_pixels),
    }


def free_render_signature(
    frame_width: int,
    max_canvas_pixels: int,
) -> dict[str, int | str]:
    return {
        "renderer": FREE_RENDERER_VERSION,
        "frame_width": int(frame_width),
        "max_canvas_pixels": int(max_canvas_pixels),
    }


def _load_render_manifest(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_render_manifest(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class PipelineRunner:
    def __init__(
        self,
        config: JobConfig,
        progress_callback: ProgressCallback | None = None,
        log_callback: LogCallback | None = None,
        control: PipelineControl | None = None,
    ) -> None:
        config.validate()
        config.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.store = JobStore(config)
        self.control = control or PipelineControl()
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.logger = create_job_logger(config.log_file)
        self.info: VideoInfo | None = None

    def _log(self, message: str) -> None:
        self.logger.info(message)
        if self.log_callback:
            self.log_callback(message)

    def _emit(
        self,
        stage: JobStage,
        progress: float,
        message: str,
    ) -> None:
        self.store.update(stage=stage, progress=progress, message=message)
        self._log(message)
        if self.progress_callback:
            self.progress_callback(str(stage), progress, message)

    def _subprogress(self, stage: JobStage, start: float, span: float):
        def callback(progress: float, message: str) -> None:
            self.control.checkpoint()
            overall = start + span * progress
            self.store.update(stage=stage, progress=overall, message=message)
            if self.progress_callback:
                self.progress_callback(str(stage), overall, message)

        return callback

    def run(self) -> dict[str, str]:
        try:
            return self._run()
        except PipelineCancelled as error:
            self.store.update(
                stage=JobStage.CANCELLED,
                message=str(error),
            )
            self._log(str(error))
            raise
        except Exception as error:
            details = traceback.format_exc()
            self.logger.error(details)
            self.store.update(
                stage=JobStage.FAILED,
                message=f"处理失败：{error}",
                error=details,
            )
            if self.progress_callback:
                self.progress_callback(
                    str(JobStage.FAILED),
                    self.store.state.progress,
                    f"处理失败：{error}",
                )
            raise

    def _run(self) -> dict[str, str]:
        # Refresh persisted options and discard a traceback left by an earlier
        # failed attempt before resuming from cached intermediate outputs.
        self.store.update(config=self.config.to_dict(), error=None)
        self._emit(JobStage.INSPECTING, 0.01, "正在读取视频参数")
        self.info = inspect_video(self.config.video_path)
        if abs(self.info.aspect_ratio - 16 / 9) > 0.08:
            self._log(
                "提示：当前版本优先针对16:9航拍视频优化，"
                "该视频的画面比例不同。"
            )
        metadata_path = self.config.work_dir / "video_info.json"
        metadata_path.write_text(
            json.dumps(self.info.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        interval = self.store.state.interval_seconds
        if interval is None:
            if self.config.interval_mode == "auto":
                self._emit(
                    JobStage.RECOMMENDING_INTERVAL,
                    0.03,
                    "正在自动评估抽帧间隔",
                )
                interval_left = (
                    0.035
                    if self.config.stitch_mode == "free"
                    else self.config.road_left_ratio
                )
                interval_right = (
                    0.965
                    if self.config.stitch_mode == "free"
                    else self.config.road_right_ratio
                )
                interval, interval_results = recommend_interval(
                    self.config.video_path,
                    self.info,
                    interval_left,
                    interval_right,
                    self.control,
                    self._subprogress(
                        JobStage.RECOMMENDING_INTERVAL,
                        0.03,
                        0.08,
                    ),
                )
                interval_report = self.config.work_dir / "interval_report.json"
                interval_report.write_text(
                    json.dumps(
                        {
                            str(key): values
                            for key, values in interval_results.items()
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            else:
                interval = self.config.interval_seconds
            self.store.update(interval_seconds=float(interval))
        interval = float(interval)
        self._log(f"采用抽帧间隔：{interval:g} 秒")

        frame_dir = self.config.frames_dir(interval)
        self._emit(JobStage.EXTRACTING, 0.12, "正在提取拼接画面")
        if not extraction_is_complete(frame_dir, self.info, interval):
            extract_frames(
                self.config.video_path,
                frame_dir,
                self.info,
                interval,
                self.config.jpeg_quality,
                self.control,
                self._subprogress(JobStage.EXTRACTING, 0.12, 0.20),
            )
        else:
            self._log("抽帧文件完整，跳过重复提取")

        project_name = safe_name(self.config.project_name)
        if self.config.stitch_mode == "free":
            while True:
                try:
                    return self._run_free_translation(frame_dir, project_name)
                except RuntimeError as error:
                    can_retry = (
                        self.config.interval_mode == "auto"
                        and "重叠不足" in str(error)
                        and interval > 0.5
                    )
                    if not can_retry:
                        raise
                    interval = max(0.5, interval / 2.0)
                    self._log(
                        f"局部画面移动过快，自动将抽帧间隔缩短为 "
                        f"{interval:g} 秒后重试"
                    )
                    self.store.update(
                        interval_seconds=interval,
                        error=None,
                    )
                    frame_dir = self.config.frames_dir(interval)
                    self._emit(
                        JobStage.EXTRACTING,
                        0.18,
                        f"正在按 {interval:g} 秒间隔重新提取画面",
                    )
                    if not extraction_is_complete(
                        frame_dir,
                        self.info,
                        interval,
                    ):
                        extract_frames(
                            self.config.video_path,
                            frame_dir,
                            self.info,
                            interval,
                            self.config.jpeg_quality,
                            self.control,
                            self._subprogress(
                                JobStage.EXTRACTING,
                                0.18,
                                0.16,
                            ),
                        )

        prefix = "app"
        rough_alignment = self.config.work_dir / f"{prefix}累计对齐矩阵.npz"
        self.control.checkpoint()
        self._emit(
            JobStage.ANALYZING_OVERLAP,
            0.34,
            "正在分析相邻画面重叠",
        )
        if not rough_alignment.is_file():
            rough_alignment = run_overlap_analysis(
                self.config.work_dir,
                frame_dir,
                prefix,
                self._log,
            )
        else:
            self._log("已有重叠分析结果，跳过重复分析")

        precise_alignment = self.config.work_dir / "precise_alignment.npz"
        self.control.checkpoint()
        self._emit(JobStage.ALIGNING, 0.48, "正在精确对齐道路标线和护栏")
        if (
            precise_alignment.is_file()
            and precise_alignment_is_current(precise_alignment)
        ):
            alignment = load_precise_alignment(
                self.config.work_dir,
                frame_dir,
                rough_alignment,
                precise_alignment,
                self.info,
            )
            self._log("已有精确对齐结果，继续渲染")
        else:
            if precise_alignment.is_file():
                self._log("道路精确对齐缓存已更新，重新进行精确对齐")
            alignment = run_precise_alignment(
                self.config.work_dir,
                frame_dir,
                rough_alignment,
                precise_alignment,
                self.info,
                self._log,
            )
        selected, homographies, rigid_transforms, displacements = alignment
        render_manifest_path = (
            self.config.work_dir / "road_render_manifest.json"
        )
        render_manifest = _load_render_manifest(render_manifest_path)

        if self.config.make_preview:
            self.control.checkpoint()
            self._emit(
                JobStage.RENDERING_PREVIEW,
                0.66,
                "正在生成弯道拼接预览",
            )
            preview = self.config.output_dir / f"{project_name}_预览.jpg"
            qa = self.config.work_dir / f"{project_name}_接缝检查.jpg"
            preview_signature = road_render_signature(
                self.config.preview_frame_width,
                self.config.preview_max_canvas_pixels,
            )
            if (
                not preview.is_file()
                or render_manifest.get("preview") != preview_signature
            ):
                render_curved_mosaic(
                    self.config.work_dir,
                    frame_dir,
                    rough_alignment,
                    precise_alignment,
                    self.info,
                    selected,
                    homographies,
                    rigid_transforms,
                    displacements,
                    self.config.preview_frame_width,
                    self.config.preview_max_canvas_pixels,
                    preview,
                    qa,
                    self._log,
                )
                render_manifest["preview"] = preview_signature
                _save_render_manifest(
                    render_manifest_path,
                    render_manifest,
                )
            self.store.add_output("preview", preview)

        if self.config.make_high_resolution:
            self.control.checkpoint()
            self._emit(
                JobStage.RENDERING_HIGH,
                0.80,
                "正在生成高清拼接图",
            )
            high = self.config.output_dir / f"{project_name}_高清.jpg"
            high_signature = road_render_signature(
                self.config.high_frame_width,
                self.config.high_max_canvas_pixels,
            )
            if (
                not high.is_file()
                or render_manifest.get("high_resolution")
                != high_signature
            ):
                render_curved_mosaic(
                    self.config.work_dir,
                    frame_dir,
                    rough_alignment,
                    precise_alignment,
                    self.info,
                    selected,
                    homographies,
                    rigid_transforms,
                    displacements,
                    self.config.high_frame_width,
                    self.config.high_max_canvas_pixels,
                    high,
                    None,
                    self._log,
                )
                render_manifest["high_resolution"] = high_signature
                _save_render_manifest(
                    render_manifest_path,
                    render_manifest,
                )
            self.store.add_output("high_resolution", high)

        self.store.update(error=None)
        self._emit(JobStage.COMPLETED, 1.0, "处理完成")
        return dict(self.store.state.outputs)

    def _run_free_translation(
        self,
        frame_dir: Path,
        project_name: str,
    ) -> dict[str, str]:
        frame_paths = sorted(frame_dir.glob("*.jpg"))
        cache_path = self.config.work_dir / "free_alignment_v1.npz"
        report_path = self.config.work_dir / "free_alignment_report.json"
        render_manifest_path = (
            self.config.work_dir / "free_render_manifest.json"
        )
        render_manifest = _load_render_manifest(render_manifest_path)

        self.control.checkpoint()
        self._emit(
            JobStage.ALIGNING,
            0.36,
            "正在进行全画面自由平移配准",
        )
        alignment = load_free_alignment(cache_path, len(frame_paths))
        if alignment is None:
            alignment = align_free_translation(
                frame_paths,
                cache_path,
                report_path,
                self.control,
                self._log,
                self._subprogress(JobStage.ALIGNING, 0.36, 0.27),
            )
        else:
            self._log("已有自由平移配准缓存，继续渲染")

        if self.config.make_preview:
            self.control.checkpoint()
            self._emit(
                JobStage.RENDERING_PREVIEW,
                0.66,
                "正在生成自由平移拼接预览",
            )
            preview = (
                self.config.output_dir
                / f"{project_name}_自由拼接_预览.jpg"
            )
            preview_signature = free_render_signature(
                self.config.preview_frame_width,
                self.config.preview_max_canvas_pixels,
            )
            if (
                not preview.is_file()
                or render_manifest.get("preview") != preview_signature
            ):
                render_free_mosaic(
                    frame_paths,
                    alignment,
                    self.config.preview_frame_width,
                    self.config.preview_max_canvas_pixels,
                    preview,
                    self.control,
                    self._log,
                    self._subprogress(
                        JobStage.RENDERING_PREVIEW,
                        0.66,
                        0.13,
                    ),
                )
                render_manifest["preview"] = preview_signature
                _save_render_manifest(
                    render_manifest_path,
                    render_manifest,
                )
            self.store.add_output("free_preview", preview)

        if self.config.make_high_resolution:
            self.control.checkpoint()
            self._emit(
                JobStage.RENDERING_HIGH,
                0.80,
                "正在生成自由平移高清拼接图",
            )
            high = (
                self.config.output_dir
                / f"{project_name}_自由拼接_高清.jpg"
            )
            high_signature = free_render_signature(
                self.config.high_frame_width,
                self.config.high_max_canvas_pixels,
            )
            if (
                not high.is_file()
                or render_manifest.get("high_resolution")
                != high_signature
            ):
                render_free_mosaic(
                    frame_paths,
                    alignment,
                    self.config.high_frame_width,
                    self.config.high_max_canvas_pixels,
                    high,
                    self.control,
                    self._log,
                    self._subprogress(
                        JobStage.RENDERING_HIGH,
                        0.80,
                        0.18,
                    ),
                )
                render_manifest["high_resolution"] = high_signature
                _save_render_manifest(
                    render_manifest_path,
                    render_manifest,
                )
            self.store.add_output("free_high_resolution", high)

        self.store.update(error=None)
        self._emit(JobStage.COMPLETED, 1.0, "自由平移拼接完成")
        return dict(self.store.state.outputs)
