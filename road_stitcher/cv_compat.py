from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType


def load_cv2() -> ModuleType:
    try:
        import cv2

        if hasattr(cv2, "VideoCapture"):
            return cv2
    except ImportError:
        pass

    local_dependencies = Path(__file__).resolve().parents[1] / ".video_deps"
    if local_dependencies.is_dir():
        sys.path.insert(0, str(local_dependencies))
        import cv2

        if hasattr(cv2, "VideoCapture"):
            return cv2
    raise RuntimeError(
        "未找到OpenCV视频组件。请安装 requirements.txt 中的依赖。"
    )


def load_numpy() -> ModuleType:
    try:
        import numpy

        return numpy
    except ImportError:
        local_dependencies = Path(__file__).resolve().parents[1] / ".video_deps"
        if local_dependencies.is_dir():
            sys.path.insert(0, str(local_dependencies))
            import numpy

            return numpy
        raise RuntimeError("未找到NumPy组件。")
