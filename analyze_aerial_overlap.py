from pathlib import Path
import argparse
import csv
import math

import cv2
import numpy as np


OUTPUT = Path(r"C:\Users\DELL\Documents\New project\航拍处理结果")
ANALYSIS_WIDTH = 1200


def imread_unicode(path: Path) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取图像：{path}")
    return image


def prepare(path: Path) -> tuple[np.ndarray, float]:
    image = imread_unicode(path)
    scale = ANALYSIS_WIDTH / image.shape[1]
    small = cv2.resize(
        image,
        (ANALYSIS_WIDTH, int(round(image.shape[0] * scale))),
        interpolation=cv2.INTER_AREA,
    )
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return gray, scale


def estimate_current_to_previous(
    detector: cv2.SIFT,
    matcher: cv2.BFMatcher,
    previous: np.ndarray,
    current: np.ndarray,
) -> tuple[np.ndarray, int, int]:
    keypoints_previous, descriptors_previous = detector.detectAndCompute(previous, None)
    keypoints_current, descriptors_current = detector.detectAndCompute(current, None)
    if descriptors_previous is None or descriptors_current is None:
        raise RuntimeError("画面特征不足，无法计算重叠")

    pairs = matcher.knnMatch(descriptors_current, descriptors_previous, k=2)
    good = [first for first, second in pairs if first.distance < 0.72 * second.distance]
    if len(good) < 12:
        raise RuntimeError(f"有效匹配点过少：{len(good)}")

    current_points = np.float32(
        [keypoints_current[match.queryIdx].pt for match in good]
    ).reshape(-1, 1, 2)
    previous_points = np.float32(
        [keypoints_previous[match.trainIdx].pt for match in good]
    ).reshape(-1, 1, 2)

    affine, inliers = cv2.estimateAffinePartial2D(
        current_points,
        previous_points,
        method=cv2.RANSAC,
        ransacReprojThreshold=3.0,
        maxIters=5000,
        confidence=0.999,
        refineIters=20,
    )
    if affine is None or inliers is None:
        raise RuntimeError("无法求得相邻画面的对齐关系")
    transform = np.vstack([affine, [0.0, 0.0, 1.0]])
    return transform, len(good), int(inliers.sum())


def overlap_fraction(transform: np.ndarray, width: int, height: int) -> float:
    current_corners = np.float32(
        [[[0, 0]], [[width, 0]], [[width, height]], [[0, height]]]
    )
    warped = cv2.perspectiveTransform(current_corners, transform).reshape(-1, 2)
    previous = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    area, _ = cv2.intersectConvexConvex(warped.astype(np.float32), previous)
    return max(0.0, float(area) / float(width * height))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", default="每5秒抽帧")
    parser.add_argument("--prefix", default="5秒")
    args = parser.parse_args()
    frame_dir = OUTPUT / args.folder

    frame_paths = sorted(frame_dir.glob("*.jpg"))
    if len(frame_paths) < 2:
        raise RuntimeError("抽帧数量不足")
    source_image = imread_unicode(frame_paths[0])
    source_height, source_width = source_image.shape[:2]

    detector = cv2.SIFT_create(nfeatures=7000, contrastThreshold=0.02)
    matcher = cv2.BFMatcher(cv2.NORM_L2)

    previous, scale = prepare(frame_paths[0])
    height, width = previous.shape
    cumulative = [np.eye(3, dtype=np.float64)]
    rows: list[dict[str, object]] = []

    for index, frame_path in enumerate(frame_paths[1:], start=1):
        current, current_scale = prepare(frame_path)
        if not math.isclose(current_scale, scale):
            raise RuntimeError("抽帧分辨率不一致")

        transform, matches, inliers = estimate_current_to_previous(
            detector, matcher, previous, current
        )
        cumulative.append(cumulative[-1] @ transform)

        a, b = transform[0, 0], transform[1, 0]
        transform_scale = math.sqrt(a * a + b * b)
        angle = math.degrees(math.atan2(b, a))
        overlap = overlap_fraction(transform, width, height)
        row = {
            "前一帧": frame_paths[index - 1].name,
            "当前帧": frame_path.name,
            "有效匹配": matches,
            "内点": inliers,
            "内点率": round(inliers / matches, 4),
            "X位移_分析像素": round(float(transform[0, 2]), 3),
            "Y位移_分析像素": round(float(transform[1, 2]), 3),
            "缩放": round(transform_scale, 6),
            "旋转角度": round(angle, 4),
            "估计重叠率": round(overlap, 4),
        }
        rows.append(row)
        print(
            f"{index:02d}->{index + 1:02d} "
            f"matches={matches:4d} inliers={inliers:4d} "
            f"overlap={overlap:5.1%} "
            f"dx={transform[0, 2]:8.1f} dy={transform[1, 2]:8.1f} "
            f"angle={angle:6.2f}"
        )
        previous = current

    csv_path = OUTPUT / f"{args.prefix}相邻画面重叠分析.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    corners = np.float64(
        [[[0, 0]], [[width, 0]], [[width, height]], [[0, height]]]
    )
    all_corners = [
        cv2.perspectiveTransform(corners.astype(np.float32), matrix).reshape(-1, 2)
        for matrix in cumulative
    ]
    points = np.vstack(all_corners)
    minimum = points.min(axis=0)
    maximum = points.max(axis=0)

    transforms_path = OUTPUT / f"{args.prefix}累计对齐矩阵.npz"
    np.savez_compressed(
        transforms_path,
        transforms=np.stack(cumulative),
        analysis_scale=np.float64(scale),
        source_width=np.int32(source_width),
        source_height=np.int32(source_height),
    )

    print(f"平均重叠率：{np.mean([float(row['估计重叠率']) for row in rows]):.1%}")
    print(
        "分析尺度拼接边界："
        f"x={minimum[0]:.1f}..{maximum[0]:.1f}, "
        f"y={minimum[1]:.1f}..{maximum[1]:.1f}"
    )
    print(f"分析表：{csv_path}")
    print(f"对齐矩阵：{transforms_path}")


if __name__ == "__main__":
    main()
