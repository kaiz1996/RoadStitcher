from pathlib import Path
import csv

import cv2
import numpy as np


OUTPUT = Path(r"C:\Users\DELL\Documents\New project\航拍处理结果")
FRAME_DIR = OUTPUT / "每2秒拼接用抽帧"
TRANSFORMS_PATH = OUTPUT / "2秒累计对齐矩阵.npz"
ANALYSIS_WIDTH = 1200.0


def imread_unicode(path: Path) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取图像：{path}")
    return image


def imwrite_unicode(path: Path, image: np.ndarray, quality: int = 93) -> None:
    ok, encoded = cv2.imencode(
        path.suffix,
        image,
        [int(cv2.IMWRITE_JPEG_QUALITY), quality],
    )
    if not ok:
        raise RuntimeError(f"无法编码图像：{path}")
    encoded.tofile(path)


def resized_frame(path: Path, width: int) -> np.ndarray:
    image = imread_unicode(path)
    height = int(round(image.shape[0] * width / image.shape[1]))
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def center_displacements(
    cumulative: np.ndarray, analysis_height: float
) -> list[float]:
    center = np.array(
        [ANALYSIS_WIDTH / 2.0, analysis_height / 2.0, 1.0],
        dtype=np.float64,
    )
    displacements: list[float] = []
    for index in range(1, len(cumulative)):
        pair = np.linalg.inv(cumulative[index - 1]) @ cumulative[index]
        mapped = pair @ center
        mapped /= mapped[2]
        displacement = center[1] - mapped[1]
        displacements.append(max(1.0, float(displacement)))
    return displacements


def smooth_displacements(values: list[float]) -> list[float]:
    result: list[float] = []
    array = np.asarray(values, dtype=np.float64)
    for index, value in enumerate(array):
        left = max(0, index - 2)
        right = min(len(array), index + 3)
        local_median = float(np.median(array[left:right]))
        lower = max(1.0, local_median * 0.72)
        upper = local_median * 1.28
        result.append(float(np.clip(value, lower, upper)))
    return result


def build_mosaic(
    frame_paths: list[Path],
    analysis_displacements: list[float],
    width: int,
    destination: Path,
) -> tuple[int, int]:
    first = resized_frame(frame_paths[0], width)
    height = first.shape[0]
    scale = width / ANALYSIS_WIDTH
    displacements = [
        max(1, min(height - 2, int(round(value * scale))))
        for value in analysis_displacements
    ]
    total_height = height + sum(displacements)
    mosaic = np.empty((total_height, width, 3), dtype=np.uint8)

    current = resized_frame(frame_paths[-1], width)
    output_y = 0
    start_row = 0
    base_blend = max(8, width // 64)

    for index in range(len(frame_paths) - 1, 0, -1):
        previous = resized_frame(frame_paths[index - 1], width)
        displacement = displacements[index - 1]
        next_displacement = (
            displacements[index - 2] if index > 1 else height
        )
        blend = min(
            base_blend,
            max(2, displacement // 5),
            max(2, next_displacement // 5),
            height - displacement,
        )

        unique = current[start_row:displacement]
        unique_height = unique.shape[0]
        if unique_height > 0:
            mosaic[output_y : output_y + unique_height] = unique
            output_y += unique_height

        current_overlap = current[displacement : displacement + blend].astype(
            np.float32
        )
        previous_overlap = previous[:blend].astype(np.float32)
        alpha = np.linspace(0.0, 1.0, blend, dtype=np.float32)[:, None, None]
        transition = np.clip(
            current_overlap * (1.0 - alpha) + previous_overlap * alpha,
            0,
            255,
        ).astype(np.uint8)
        mosaic[output_y : output_y + blend] = transition
        output_y += blend

        current = previous
        start_row = blend
        if index % 10 == 0 or index == 1:
            print(
                f"已处理到第 {index:02d} 帧，"
                f"当前高度 {output_y}/{total_height}"
            )

    remainder = current[start_row:]
    mosaic[output_y : output_y + remainder.shape[0]] = remainder
    output_y += remainder.shape[0]
    if output_y != total_height:
        raise RuntimeError(
            f"拼接高度计算不一致：实际 {output_y}，预计 {total_height}"
        )

    imwrite_unicode(destination, mosaic, quality=93)
    return width, total_height


def main() -> None:
    frame_paths = sorted(FRAME_DIR.glob("*.jpg"))
    if len(frame_paths) < 2:
        raise RuntimeError("拼接素材不足")

    alignment = np.load(TRANSFORMS_PATH)
    cumulative = alignment["transforms"]
    source_height = float(alignment["source_height"])
    source_width = float(alignment["source_width"])
    analysis_scale = float(alignment["analysis_scale"])
    analysis_height = source_height * analysis_scale
    if len(cumulative) != len(frame_paths):
        raise RuntimeError("抽帧数量与对齐数据不一致")
    if not np.isclose(source_width * analysis_scale, ANALYSIS_WIDTH):
        raise RuntimeError("分析数据尺度不一致")

    raw = center_displacements(cumulative, analysis_height)
    smoothed = smooth_displacements(raw)

    report_path = OUTPUT / "条带拼接位移.csv"
    with report_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["前一帧", "当前帧", "原始位移_分析像素", "采用位移_分析像素"]
        )
        for index, (raw_value, used_value) in enumerate(
            zip(raw, smoothed), start=1
        ):
            writer.writerow(
                [
                    frame_paths[index - 1].name,
                    frame_paths[index].name,
                    round(raw_value, 3),
                    round(used_value, 3),
                ]
            )

    preview_path = OUTPUT / "整条道路拼接_预览.jpg"
    preview_size = build_mosaic(
        frame_paths,
        smoothed,
        width=768,
        destination=preview_path,
    )
    print(f"预览图完成：{preview_path}，{preview_size[0]}×{preview_size[1]}")

    final_path = OUTPUT / "整条道路拼接_高清.jpg"
    final_size = build_mosaic(
        frame_paths,
        smoothed,
        width=1536,
        destination=final_path,
    )
    print(f"高清图完成：{final_path}，{final_size[0]}×{final_size[1]}")

    notes_path = OUTPUT / "处理说明.txt"
    notes_path.write_text(
        "\n".join(
            [
                "处理结果说明",
                "",
                "1. 已按用户要求每隔5秒提取36张原分辨率画面。",
                "2. 实测匀速阶段5秒间隔缺少可靠重叠，因此另按2秒间隔提取89张画面用于拼接。",
                "3. 拼接图采用道路拉直式条带拼接；路线终点位于成图上方，起点位于成图下方。",
                "4. 高清拼接图宽度为原视频的40%，原始2秒抽帧仍保留3840×2160分辨率。",
                "5. 航拍视频不是测绘正射数据，树冠、车辆和高层建筑在接缝附近可能存在少量视差。",
                "6. 高清图纵向尺寸超过普通PSD的30000像素限制，在Photoshop中请保存为PSB大型文档格式。",
                "",
                f"预览图：{preview_path.name}（{preview_size[0]}×{preview_size[1]}）",
                f"高清图：{final_path.name}（{final_size[0]}×{final_size[1]}）",
            ]
        ),
        encoding="utf-8-sig",
    )


if __name__ == "__main__":
    main()
