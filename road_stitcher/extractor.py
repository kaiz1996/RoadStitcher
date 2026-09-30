from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

from .cv_compat import load_cv2
from .media import VideoInfo, imwrite_unicode
from .state import PipelineControl


def expected_frame_count(info: VideoInfo, interval_seconds: float) -> int:
    return int(math.ceil(info.duration_seconds / interval_seconds))


def extraction_is_complete(
    frame_dir: Path,
    info: VideoInfo,
    interval_seconds: float,
) -> bool:
    expected = expected_frame_count(info, interval_seconds)
    actual = len(list(frame_dir.glob("frame_*.jpg")))
    return actual == expected


def extract_frames(
    video_path: Path,
    frame_dir: Path,
    info: VideoInfo,
    interval_seconds: float,
    quality: int,
    control: PipelineControl,
    progress: Callable[[float, str], None] | None = None,
) -> list[Path]:
    cv2 = load_cv2()
    frame_dir.mkdir(parents=True, exist_ok=True)
    total = expected_frame_count(info, interval_seconds)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")
    paths: list[Path] = []
    try:
        for index in range(total):
            control.checkpoint()
            timestamp = index * interval_seconds
            path = frame_dir / f"frame_{index + 1:04d}_{timestamp:08.2f}s.jpg"
            if path.is_file():
                paths.append(path)
            else:
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
                ok, frame = capture.read()
                if not ok:
                    raise RuntimeError(
                        f"无法读取 {timestamp:.2f} 秒处的视频画面"
                    )
                imwrite_unicode(path, frame, quality)
                paths.append(path)
            if progress:
                progress(
                    (index + 1) / total,
                    f"抽取画面 {index + 1}/{total}",
                )
    finally:
        capture.release()
    return paths
