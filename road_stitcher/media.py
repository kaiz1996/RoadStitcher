from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .cv_compat import load_cv2, load_numpy


@dataclass(frozen=True, slots=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    frame_count: int
    duration_seconds: float

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def inspect_video(path: Path) -> VideoInfo:
    cv2 = load_cv2()
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{path}")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        capture.release()
    if fps <= 0 or frame_count <= 0 or width <= 0 or height <= 0:
        raise RuntimeError("视频参数无效或编码格式不受支持")
    return VideoInfo(
        width=width,
        height=height,
        fps=fps,
        frame_count=frame_count,
        duration_seconds=frame_count / fps,
    )


def imread_unicode(path: Path):
    cv2 = load_cv2()
    np = load_numpy()
    image = cv2.imdecode(
        np.fromfile(path, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if image is None:
        raise RuntimeError(f"无法读取图像：{path}")
    return image


def imwrite_unicode(
    path: Path,
    image,
    quality: int = 95,
) -> None:
    cv2 = load_cv2()
    path.parent.mkdir(parents=True, exist_ok=True)
    parameters: list[int] = []
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        parameters = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    ok, encoded = cv2.imencode(path.suffix, image, parameters)
    if not ok:
        raise RuntimeError(f"无法编码图像：{path}")
    encoded.tofile(path)
