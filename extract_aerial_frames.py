from pathlib import Path
import argparse

import cv2
import numpy as np


VIDEO = Path(r"C:\Users\DELL\Downloads\航拍.mp4")
OUTPUT = Path(r"C:\Users\DELL\Documents\New project\航拍处理结果")


def imwrite_unicode(path: Path, image: np.ndarray, params: list[int]) -> None:
    suffix = path.suffix
    ok, encoded = cv2.imencode(suffix, image, params)
    if not ok:
        raise RuntimeError(f"无法编码图像：{path}")
    encoded.tofile(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--folder", default="每5秒抽帧")
    parser.add_argument("--contact-name", default="抽帧总览.jpg")
    args = parser.parse_args()
    interval_seconds = args.interval
    frame_dir = OUTPUT / args.folder

    OUTPUT.mkdir(parents=True, exist_ok=True)
    frame_dir.mkdir(parents=True, exist_ok=True)

    capture = cv2.VideoCapture(str(VIDEO))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{VIDEO}")

    fps = capture.get(cv2.CAP_PROP_FPS)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = frame_count / fps

    timestamps = np.arange(0.0, duration, interval_seconds)
    extracted: list[tuple[float, Path, np.ndarray]] = []

    for index, timestamp in enumerate(timestamps, start=1):
        capture.set(cv2.CAP_PROP_POS_MSEC, float(timestamp * 1000.0))
        ok, frame = capture.read()
        if not ok:
            print(f"跳过无法读取的时间点：{timestamp:.1f}s")
            continue
        frame_path = frame_dir / f"道路_{index:03d}_{timestamp:06.1f}s.jpg"
        imwrite_unicode(
            frame_path,
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), 97],
        )
        thumbnail = cv2.resize(frame, (640, 360), interpolation=cv2.INTER_AREA)
        extracted.append((timestamp, frame_path, thumbnail))
        print(f"[{index:02d}/{len(timestamps):02d}] {timestamp:6.1f}s -> {frame_path.name}")

    capture.release()

    columns = 4
    rows = int(np.ceil(len(extracted) / columns))
    label_height = 34
    sheet = np.full(
        (rows * (360 + label_height), columns * 640, 3),
        245,
        dtype=np.uint8,
    )
    for item_index, (timestamp, _, thumbnail) in enumerate(extracted):
        row, column = divmod(item_index, columns)
        x0 = column * 640
        y0 = row * (360 + label_height)
        sheet[y0 : y0 + 360, x0 : x0 + 640] = thumbnail
        cv2.putText(
            sheet,
            f"#{item_index + 1:02d}  {timestamp:.1f}s",
            (x0 + 12, y0 + 384),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (30, 30, 30),
            2,
            cv2.LINE_AA,
        )

    contact_path = OUTPUT / args.contact_name
    imwrite_unicode(
        contact_path,
        sheet,
        [int(cv2.IMWRITE_JPEG_QUALITY), 92],
    )

    info_path = OUTPUT / "视频与抽帧信息.txt"
    info_path.write_text(
        "\n".join(
            [
                f"原视频：{VIDEO}",
                f"分辨率：{width} × {height}",
                f"帧率：{fps:.3f} fps",
                f"时长：{duration:.3f} 秒",
                f"抽帧间隔：{interval_seconds:.1f} 秒",
                f"抽帧数量：{len(extracted)}",
            ]
        ),
        encoding="utf-8-sig",
    )

    print(f"完成：共提取 {len(extracted)} 张")
    print(f"抽帧目录：{frame_dir}")
    print(f"总览图：{contact_path}")


if __name__ == "__main__":
    main()
