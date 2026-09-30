from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from road_stitcher.free_stitch import (
    align_free_translation,
    render_free_mosaic,
)
from road_stitcher.state import PipelineControl


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("frames", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--frame-width", type=int, default=600)
    args = parser.parse_args()

    frame_paths = sorted(args.frames.glob("*.jpg"))
    args.output.mkdir(parents=True, exist_ok=True)
    control = PipelineControl()

    alignment = align_free_translation(
        frame_paths,
        args.output / "free_alignment_v1.npz",
        args.output / "free_alignment_report.json",
        control,
        lambda message: print(message, flush=True),
    )
    print(f"SELECTED={len(alignment.selected)}", flush=True)
    result = render_free_mosaic(
        frame_paths,
        alignment,
        args.frame_width,
        45_000_000,
        args.output / "free_mosaic_preview.jpg",
        control,
        lambda message: print(message, flush=True),
    )
    print(f"RESULT={result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
