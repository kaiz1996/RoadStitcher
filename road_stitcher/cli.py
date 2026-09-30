from __future__ import annotations

import argparse
import faulthandler
import json
import os
import sys
from pathlib import Path

from .config import JobConfig
from .pipeline import PipelineRunner
from .ui import run_gui


_CRASH_LOG_HANDLE = None


def _enable_crash_log() -> None:
    global _CRASH_LOG_HANDLE
    if _CRASH_LOG_HANDLE is not None:
        return
    try:
        local_data = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        log_dir = local_data / "RoadStitcher"
        log_dir.mkdir(parents=True, exist_ok=True)
        _CRASH_LOG_HANDLE = (log_dir / "native_crash.log").open(
            "a",
            encoding="utf-8",
        )
        faulthandler.enable(_CRASH_LOG_HANDLE, all_threads=True)
    except (OSError, RuntimeError):
        _CRASH_LOG_HANDLE = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="航拍道路拼接工具")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="以命令行方式处理视频")
    run_parser.add_argument("video", type=Path)
    run_parser.add_argument("output", type=Path)
    run_parser.add_argument("--name", default="航拍道路拼接")
    run_parser.add_argument(
        "--mode",
        choices=["road", "free"],
        default="road",
        help="road=沿道路拼接，free=自由平移拼接",
    )
    run_parser.add_argument("--interval", type=float)
    run_parser.add_argument("--preview-only", action="store_true")
    run_parser.add_argument("--high-only", action="store_true")

    subparsers.add_parser("gui", help="启动图形界面")
    return parser


def main(arguments: list[str] | None = None) -> int:
    _enable_crash_log()
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.command in {None, "gui"}:
        return run_gui()

    make_preview = not args.high_only
    make_high = not args.preview_only
    config = JobConfig(
        video_path=args.video,
        output_dir=args.output,
        project_name=args.name,
        stitch_mode=args.mode,
        interval_mode="manual" if args.interval else "auto",
        interval_seconds=args.interval or 2.0,
        make_preview=make_preview,
        make_high_resolution=make_high,
    )

    console_available = sys.stdout is not None

    def progress(stage: str, value: float, message: str) -> None:
        if console_available:
            print(f"[{value:6.1%}] {message}")

    runner = PipelineRunner(
        config,
        progress_callback=progress if console_available else None,
        log_callback=print if console_available else None,
    )
    outputs = runner.run()
    if console_available:
        print(json.dumps(outputs, ensure_ascii=False, indent=2))
    return 0
