from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from typing import Callable

from .media import VideoInfo


class _LogWriter(io.TextIOBase):
    def __init__(self, callback: Callable[[str], None]) -> None:
        self.callback = callback
        self._buffer = ""
        self._original_stdout = sys.stdout

    def _dispatch(self, line: str) -> None:
        with contextlib.redirect_stdout(self._original_stdout):
            self.callback(line)

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._dispatch(line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._dispatch(self._buffer.rstrip())
        self._buffer = ""


@contextlib.contextmanager
def _temporary_argv(arguments: list[str]):
    previous = sys.argv[:]
    sys.argv = arguments
    try:
        yield
    finally:
        sys.argv = previous


def run_overlap_analysis(
    output_dir: Path,
    frame_dir: Path,
    prefix: str,
    log: Callable[[str], None],
) -> Path:
    import analyze_aerial_overlap as analyzer

    analyzer.OUTPUT = output_dir
    writer = _LogWriter(log)
    arguments = [
        "analyze_aerial_overlap.py",
        "--folder",
        frame_dir.name,
        "--prefix",
        prefix,
    ]
    with _temporary_argv(arguments), contextlib.redirect_stdout(writer):
        analyzer.main()
    writer.flush()
    return output_dir / f"{prefix}累计对齐矩阵.npz"


def run_precise_alignment(
    output_dir: Path,
    frame_dir: Path,
    rough_alignment: Path,
    precise_alignment: Path,
    info: VideoInfo,
    log: Callable[[str], None],
):
    import optimize_aerial_road_stitch as optimizer

    optimizer.OUTPUT = output_dir
    optimizer.FRAME_DIR = frame_dir
    optimizer.OLD_ALIGNMENT = rough_alignment
    optimizer.NEW_ALIGNMENT = precise_alignment
    optimizer.SOURCE_WIDTH = info.width
    optimizer.SOURCE_HEIGHT = info.height
    writer = _LogWriter(log)
    frame_paths = sorted(frame_dir.glob("*.jpg"))
    with contextlib.redirect_stdout(writer):
        result = optimizer.estimate_all(frame_paths)
    writer.flush()
    return result


def load_precise_alignment(
    output_dir: Path,
    frame_dir: Path,
    rough_alignment: Path,
    precise_alignment: Path,
    info: VideoInfo,
):
    import optimize_aerial_road_stitch as optimizer

    optimizer.OUTPUT = output_dir
    optimizer.FRAME_DIR = frame_dir
    optimizer.OLD_ALIGNMENT = rough_alignment
    optimizer.NEW_ALIGNMENT = precise_alignment
    optimizer.SOURCE_WIDTH = info.width
    optimizer.SOURCE_HEIGHT = info.height
    return optimizer.load_alignment()


def precise_alignment_is_current(precise_alignment: Path) -> bool:
    """Keep pipeline cache decisions in sync with the road alignment engine."""
    import optimize_aerial_road_stitch as optimizer

    return optimizer.alignment_is_current(precise_alignment)


def render_curved_mosaic(
    output_dir: Path,
    frame_dir: Path,
    rough_alignment: Path,
    precise_alignment: Path,
    info: VideoInfo,
    selected,
    homographies,
    rigid_transforms,
    displacements,
    desired_frame_width: int,
    max_canvas_pixels: int,
    destination: Path,
    qa_destination: Path | None,
    log: Callable[[str], None],
) -> tuple[int, int, int]:
    import optimize_aerial_road_stitch as optimizer

    optimizer.OUTPUT = output_dir
    optimizer.FRAME_DIR = frame_dir
    optimizer.OLD_ALIGNMENT = rough_alignment
    optimizer.NEW_ALIGNMENT = precise_alignment
    optimizer.SOURCE_WIDTH = info.width
    optimizer.SOURCE_HEIGHT = info.height
    writer = _LogWriter(log)
    frame_paths = sorted(frame_dir.glob("*.jpg"))
    with contextlib.redirect_stdout(writer):
        result = optimizer.build_curved_mosaic(
            frame_paths,
            selected,
            homographies,
            rigid_transforms,
            displacements,
            desired_frame_width=desired_frame_width,
            max_canvas_pixels=max_canvas_pixels,
            destination=destination,
            qa_destination=qa_destination,
        )
    writer.flush()
    return result
