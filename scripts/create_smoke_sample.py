from __future__ import annotations

import argparse
from pathlib import Path

import cv2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--seconds", type=float, default=24.0)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--fps", type=float, default=10.0)
    args = parser.parse_args()

    capture = cv2.VideoCapture(str(args.source))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{args.source}")
    source_fps = capture.get(cv2.CAP_PROP_FPS)
    source_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    source_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    height = int(round(source_height * args.width / source_width))

    args.destination.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(
        str(args.destination),
        cv2.VideoWriter_fourcc(*"mp4v"),
        args.fps,
        (args.width, height),
    )
    if not writer.isOpened():
        capture.release()
        raise RuntimeError("无法创建测试视频")

    output_frames = int(round(args.seconds * args.fps))
    source_step = source_fps / args.fps
    next_source_index = 0.0
    source_index = 0
    written = 0
    try:
        while written < output_frames:
            ok, frame = capture.read()
            if not ok:
                break
            if source_index + 0.5 < next_source_index:
                source_index += 1
                continue
            resized = cv2.resize(
                frame,
                (args.width, height),
                interpolation=cv2.INTER_AREA,
            )
            writer.write(resized)
            written += 1
            next_source_index += source_step
            source_index += 1
    finally:
        writer.release()
        capture.release()
    print(args.destination)


if __name__ == "__main__":
    main()
