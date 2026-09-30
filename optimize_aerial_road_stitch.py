from pathlib import Path
import argparse
import csv
import math

import cv2
import numpy as np


OUTPUT = Path(r"C:\Users\DELL\Documents\New project\航拍处理结果")
FRAME_DIR = OUTPUT / "每2秒拼接用抽帧"
OLD_ALIGNMENT = OUTPUT / "2秒累计对齐矩阵.npz"
NEW_ALIGNMENT = OUTPUT / "优化对齐数据.npz"
ANALYSIS_WIDTH = 1200
SOURCE_WIDTH = 3840
SOURCE_HEIGHT = 2160
ROAD_ALIGNMENT_VERSION = "road-adaptive-direction-v2"
MIN_PAIR_DISPLACEMENT = 5.0
# Allow the roughly 30% overlap that can still be registered reliably.  A
# larger jump is normally a sparse-keyframe gap and is split before rendering.
MAX_PAIR_DISPLACEMENT_RATIO = 0.85


def imread_unicode(path: Path) -> np.ndarray:
    data = np.fromfile(path, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"无法读取图像：{path}")
    return image


def imwrite_unicode(path: Path, image: np.ndarray, quality: int = 94) -> None:
    ok, encoded = cv2.imencode(
        path.suffix,
        image,
        [int(cv2.IMWRITE_JPEG_QUALITY), quality],
    )
    if not ok:
        raise RuntimeError(f"无法编码图像：{path}")
    encoded.tofile(path)


def resize_to_width(image: np.ndarray, width: int) -> np.ndarray:
    height = int(round(image.shape[0] * width / image.shape[1]))
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def read_resized(path: Path, width: int) -> np.ndarray:
    return resize_to_width(imread_unicode(path), width)


def existing_displacements(frame_count: int) -> np.ndarray:
    data = np.load(OLD_ALIGNMENT)
    cumulative = data["transforms"]
    source_width = float(data["source_width"])
    source_height = float(data["source_height"])
    analysis_scale = float(data["analysis_scale"])
    if len(cumulative) != frame_count:
        raise RuntimeError("原对齐数据与抽帧数量不一致")
    center = np.array(
        [
            source_width * analysis_scale / 2.0,
            source_height * analysis_scale / 2.0,
            1.0,
        ],
        dtype=np.float64,
    )
    values = []
    for index in range(1, frame_count):
        pair = np.linalg.inv(cumulative[index - 1]) @ cumulative[index]
        mapped = pair @ center
        mapped /= mapped[2]
        values.append(max(1.0, center[1] - mapped[1]))
    values = np.asarray(values, dtype=np.float64)
    smoothed = values.copy()
    for index, value in enumerate(values):
        local = values[max(0, index - 2) : min(len(values), index + 3)]
        median = float(np.median(local))
        smoothed[index] = np.clip(value, max(1.0, median * 0.72), median * 1.28)
    return smoothed


def select_frames(frame_count: int) -> np.ndarray:
    pair_moves = existing_displacements(frame_count)
    positions = np.concatenate([[0.0], np.cumsum(pair_moves)])
    selected = [0]
    minimum_move = 180.0
    for index in range(1, frame_count - 1):
        if positions[index] - positions[selected[-1]] >= minimum_move:
            selected.append(index)
    if selected[-1] != frame_count - 1:
        selected.append(frame_count - 1)
    return np.asarray(selected, dtype=np.int32)


def road_mask(shape: tuple[int, int], wide: bool = False) -> np.ndarray:
    height, width = shape
    mask = np.zeros((height, width), dtype=np.uint8)
    if wide:
        left, right = int(width * 0.12), int(width * 0.88)
    else:
        left, right = int(width * 0.24), int(width * 0.76)
    mask[:, left:right] = 255
    return mask


def estimate_homography(
    previous: np.ndarray,
    current: np.ndarray,
    detector: cv2.SIFT,
    matcher: cv2.BFMatcher,
) -> tuple[np.ndarray, np.ndarray, int, int, float]:
    previous_gray = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)
    current_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)

    def match_with_mask(wide: bool) -> tuple[list, list, list]:
        mask = road_mask(previous_gray.shape, wide=wide)
        kp_previous, desc_previous = detector.detectAndCompute(previous_gray, mask)
        kp_current, desc_current = detector.detectAndCompute(current_gray, mask)
        if desc_previous is None or desc_current is None:
            return [], [], []
        pairs = matcher.knnMatch(desc_current, desc_previous, k=2)
        good = [
            first
            for first, second in pairs
            if first.distance < 0.76 * second.distance
        ]
        return kp_previous, kp_current, good

    kp_previous, kp_current, good = match_with_mask(wide=False)
    if len(good) < 40:
        kp_previous, kp_current, good = match_with_mask(wide=True)
    if len(good) < 16:
        raise RuntimeError(f"道路区域匹配点不足：{len(good)}")

    current_points = np.float32(
        [kp_current[item.queryIdx].pt for item in good]
    ).reshape(-1, 1, 2)
    previous_points = np.float32(
        [kp_previous[item.trainIdx].pt for item in good]
    ).reshape(-1, 1, 2)

    method = getattr(cv2, "USAC_MAGSAC", cv2.RANSAC)
    homography, inliers = cv2.findHomography(
        current_points,
        previous_points,
        method,
        3.0,
        maxIters=10000,
        confidence=0.999,
    )
    if homography is None or inliers is None or int(inliers.sum()) < 12:
        affine, affine_inliers = cv2.estimateAffinePartial2D(
            current_points,
            previous_points,
            method=cv2.RANSAC,
            ransacReprojThreshold=3.0,
            maxIters=10000,
            confidence=0.999,
            refineIters=30,
        )
        if affine is None or affine_inliers is None:
            raise RuntimeError("无法求得道路区域对齐关系")
        homography = np.vstack([affine, [0.0, 0.0, 1.0]])
        inliers = affine_inliers

    homography = homography.astype(np.float64)
    homography /= homography[2, 2]
    inlier_mask = inliers.ravel().astype(bool)
    projected = cv2.perspectiveTransform(
        current_points[inlier_mask], homography
    )
    error = float(
        np.median(
            np.linalg.norm(
                projected.reshape(-1, 2)
                - previous_points[inlier_mask].reshape(-1, 2),
                axis=1,
            )
        )
    )
    affine, _ = cv2.estimateAffinePartial2D(
        current_points[inlier_mask],
        previous_points[inlier_mask],
        method=cv2.LMEDS,
    )
    if affine is None:
        raise RuntimeError("无法求得道路转向关系")
    angle = math.atan2(float(affine[1, 0]), float(affine[0, 0]))
    cosine, sine = math.cos(angle), math.sin(angle)
    rotation = np.array(
        [[cosine, -sine], [sine, cosine]], dtype=np.float64
    )
    center = np.array(
        [previous.shape[1] / 2.0, previous.shape[0] / 2.0],
        dtype=np.float64,
    )
    mapped_center = cv2.perspectiveTransform(
        center.reshape(1, 1, 2).astype(np.float32), homography
    ).reshape(2)
    translation = mapped_center - rotation @ center
    rigid = np.array(
        [
            [rotation[0, 0], rotation[0, 1], translation[0]],
            [rotation[1, 0], rotation[1, 1], translation[1]],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return homography, rigid, len(good), int(inlier_mask.sum()), error


def center_displacement(homography: np.ndarray, height: int) -> float:
    point = np.array([ANALYSIS_WIDTH / 2.0, height / 2.0, 1.0])
    mapped = homography @ point
    mapped /= mapped[2]
    return float(point[1] - mapped[1])


def _estimate_all_legacy(
    frame_paths: list[Path],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    selected = select_frames(len(frame_paths))
    detector = cv2.SIFT_create(nfeatures=9000, contrastThreshold=0.015)
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    homographies: list[np.ndarray] = []
    rigid_transforms: list[np.ndarray] = []
    displacements: list[float] = []
    rows: list[list[object]] = []

    previous = read_resized(frame_paths[int(selected[0])], ANALYSIS_WIDTH)
    for pair_index in range(1, len(selected)):
        previous_index = int(selected[pair_index - 1])
        current_index = int(selected[pair_index])
        current = read_resized(frame_paths[current_index], ANALYSIS_WIDTH)
        homography, rigid, matches, inliers, error = estimate_homography(
            previous, current, detector, matcher
        )
        displacement = center_displacement(homography, previous.shape[0])
        if not 45.0 <= displacement <= 620.0:
            raise RuntimeError(
                f"第{previous_index + 1}到{current_index + 1}帧位移异常："
                f"{displacement:.1f}"
            )
        homographies.append(homography)
        rigid_transforms.append(rigid)
        displacements.append(displacement)
        angle = math.degrees(math.atan2(rigid[1, 0], rigid[0, 0]))
        rows.append(
            [
                previous_index + 1,
                current_index + 1,
                matches,
                inliers,
                round(inliers / matches, 4),
                round(error, 3),
                round(displacement, 3),
                round(angle, 4),
            ]
        )
        print(
            f"{previous_index + 1:02d}->{current_index + 1:02d} "
            f"matches={matches:4d} inliers={inliers:4d} "
            f"error={error:4.2f}px move={displacement:6.1f}px "
            f"angle={angle:6.2f}deg"
        )
        previous = current

    homography_array = np.stack(homographies)
    rigid_array = np.stack(rigid_transforms)
    displacement_array = np.asarray(displacements, dtype=np.float64)
    np.savez_compressed(
        NEW_ALIGNMENT,
        selected=selected,
        homographies=homography_array,
        rigid_transforms=rigid_array,
        displacements=displacement_array,
    )

    report_path = OUTPUT / "优化对齐质量.csv"
    with report_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "前一帧序号",
                "当前帧序号",
                "有效匹配",
                "内点",
                "内点率",
                "中位对齐误差_像素",
                "中心纵向位移_像素",
                "相邻转角_度",
            ]
        )
        writer.writerows(rows)
    return selected, homography_array, rigid_array, displacement_array


def estimate_all(
    frame_paths: list[Path],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Precisely align road frames, recovering automatically from sparse gaps."""
    selected = [int(value) for value in select_frames(len(frame_paths))]
    if len(selected) < 2:
        raise RuntimeError("Road stitching needs at least two extracted frames.")
    detector = cv2.SIFT_create(nfeatures=9000, contrastThreshold=0.015)
    matcher = cv2.BFMatcher(cv2.NORM_L2)

    # Normalize frame order for the forward-only road renderer.  The rough
    # alignment merely chooses keyframes and may originate from reverse flight.
    first_previous = read_resized(frame_paths[selected[0]], ANALYSIS_WIDTH)
    first_current = read_resized(frame_paths[selected[1]], ANALYSIS_WIDTH)
    first_homography, _, _, _, _ = estimate_homography(
        first_previous, first_current, detector, matcher
    )
    if center_displacement(first_homography, first_previous.shape[0]) < 0:
        selected.reverse()
        print("Detected reverse flight direction; reordered road frames.")

    homographies: list[np.ndarray] = []
    rigid_transforms: list[np.ndarray] = []
    displacements: list[float] = []
    rows: list[list[object]] = []
    previous = read_resized(frame_paths[selected[0]], ANALYSIS_WIDTH)
    pair_index = 1
    while pair_index < len(selected):
        previous_index = selected[pair_index - 1]
        current_index = selected[pair_index]
        current = read_resized(frame_paths[current_index], ANALYSIS_WIDTH)
        homography, rigid, matches, inliers, error = estimate_homography(
            previous, current, detector, matcher
        )
        displacement = center_displacement(homography, previous.shape[0])
        maximum_displacement = previous.shape[0] * MAX_PAIR_DISPLACEMENT_RATIO
        if not MIN_PAIR_DISPLACEMENT <= displacement <= maximum_displacement:
            frame_gap = abs(current_index - previous_index)
            if frame_gap > 1:
                midpoint = (previous_index + current_index) // 2
                selected.insert(pair_index, midpoint)
                print(
                    f"Frames {previous_index + 1}->{current_index + 1} "
                    f"move {displacement:.1f}px; inserted frame "
                    f"{midpoint + 1} and retrying."
                )
                continue
            raise RuntimeError(
                f"Frames {previous_index + 1}->{current_index + 1} "
                f"have abnormal movement: {displacement:.1f}px. "
                "Try a shorter extraction interval, or trim takeoff/landing footage."
            )

        homographies.append(homography)
        rigid_transforms.append(rigid)
        displacements.append(displacement)
        angle = math.degrees(math.atan2(rigid[1, 0], rigid[0, 0]))
        rows.append(
            [
                previous_index + 1,
                current_index + 1,
                matches,
                inliers,
                round(inliers / matches, 4),
                round(error, 3),
                round(displacement, 3),
                round(angle, 4),
            ]
        )
        print(
            f"{previous_index + 1:02d}->{current_index + 1:02d} "
            f"matches={matches:4d} inliers={inliers:4d} "
            f"error={error:4.2f}px move={displacement:6.1f}px "
            f"angle={angle:6.2f}deg"
        )
        previous = current
        pair_index += 1

    selected_array = np.asarray(selected, dtype=np.int32)
    homography_array = np.stack(homographies)
    rigid_array = np.stack(rigid_transforms)
    displacement_array = np.asarray(displacements, dtype=np.float64)
    np.savez_compressed(
        NEW_ALIGNMENT,
        selected=selected_array,
        homographies=homography_array,
        rigid_transforms=rigid_array,
        displacements=displacement_array,
        engine_version=np.array(ROAD_ALIGNMENT_VERSION),
    )
    report_path = OUTPUT / "road_alignment_quality.csv"
    with report_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "previous_frame",
                "current_frame",
                "matches",
                "inliers",
                "inlier_ratio",
                "median_error_px",
                "vertical_displacement_px",
                "relative_angle_deg",
            ]
        )
        writer.writerows(rows)
    return selected_array, homography_array, rigid_array, displacement_array


def alignment_is_current(path: Path | None = None) -> bool:
    """Whether a precise-road cache was created by the current aligner."""
    target = path or NEW_ALIGNMENT
    try:
        with np.load(target, allow_pickle=False) as data:
            return (
                "engine_version" in data.files
                and str(data["engine_version"].item()) == ROAD_ALIGNMENT_VERSION
            )
    except (OSError, ValueError, KeyError):
        return False


def load_alignment() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if not alignment_is_current():
        raise RuntimeError("The precise-road alignment cache is outdated.")
    data = np.load(NEW_ALIGNMENT)
    return (
        data["selected"],
        data["homographies"],
        data["rigid_transforms"],
        data["displacements"],
    )


def scale_homography(homography: np.ndarray, width: int) -> np.ndarray:
    scale = width / ANALYSIS_WIDTH
    matrix = np.diag([scale, scale, 1.0])
    return matrix @ homography @ np.linalg.inv(matrix)


def smoothstep(values: np.ndarray) -> np.ndarray:
    return values * values * (3.0 - 2.0 * values)


def frame_edge_weight(height: int, width: int) -> np.ndarray:
    """Fade source-frame borders so warp interpolation cannot draw seam lines."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    distance = np.minimum.reduce(
        [
            xx,
            yy,
            width - 1.0 - xx,
            height - 1.0 - yy,
        ]
    )
    feather = max(8.0, min(height, width) / 18.0)
    return smoothstep(np.clip(distance / feather, 0.0, 1.0))


def elastic_warp_piece(
    image: np.ndarray,
    homography: np.ndarray,
    displacement: int,
    overlap: int,
) -> np.ndarray:
    output_height = displacement + overlap
    height, width = image.shape[:2]
    yy, xx = np.mgrid[0:output_height, 0:width].astype(np.float32)
    source_x = xx.copy()
    source_y = yy.copy()
    shifted = np.array(
        [[1.0, 0.0, 0.0], [0.0, 1.0, float(displacement)], [0.0, 0.0, 1.0]]
    ) @ homography

    for _ in range(8):
        denominator = (
            shifted[2, 0] * source_x
            + shifted[2, 1] * source_y
            + shifted[2, 2]
        )
        mapped_x = (
            shifted[0, 0] * source_x
            + shifted[0, 1] * source_y
            + shifted[0, 2]
        ) / denominator
        mapped_y = (
            shifted[1, 0] * source_x
            + shifted[1, 1] * source_y
            + shifted[1, 2]
        ) / denominator
        weight = smoothstep(
            np.clip(source_y / max(1.0, float(displacement)), 0.0, 1.0)
        )
        forward_x = source_x + weight * (mapped_x - source_x)
        forward_y = source_y + weight * (mapped_y - source_y)
        source_x += (xx - forward_x) * 0.88
        source_y += (yy - forward_y) * 0.88

    return cv2.remap(
        image,
        source_x,
        source_y,
        interpolation=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REFLECT_101,
    )


def color_match(
    warped: np.ndarray,
    previous_top: np.ndarray,
    displacement: int,
    overlap: int,
) -> np.ndarray:
    current_overlap = warped[displacement : displacement + overlap]
    margin = max(4, current_overlap.shape[1] // 8)
    current_sample = current_overlap[:, margin:-margin].astype(np.float32)
    previous_sample = previous_top[:, margin:-margin].astype(np.float32)
    difference = previous_sample - current_sample
    offsets = np.median(difference.reshape(-1, 3), axis=0)
    offsets = np.clip(offsets, -14.0, 14.0)

    rows = np.arange(warped.shape[0], dtype=np.float32)
    strength = smoothstep(
        np.clip(rows / max(1.0, float(displacement)), 0.0, 1.0)
    )[:, None, None]
    corrected = warped.astype(np.float32) + strength * offsets[None, None, :]
    return np.clip(corrected, 0, 255).astype(np.uint8)


def minimum_cost_seam(
    current: np.ndarray,
    previous: np.ndarray,
    feather: int,
) -> np.ndarray:
    current_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY).astype(np.float32)
    previous_gray = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY).astype(np.float32)
    cost = cv2.GaussianBlur(
        np.abs(current_gray - previous_gray), (0, 0), 1.2
    )
    height, width = cost.shape
    vertical = np.abs(
        np.arange(height, dtype=np.float32) - (height - 1) / 2.0
    )
    vertical /= max(1.0, height / 2.0)
    cost += vertical[:, None] * max(1.0, float(np.median(cost)) * 0.22)

    accumulated = cost[:, 0].copy()
    parents = np.zeros((height, width), dtype=np.int8)
    large = np.float32(1e12)
    for x in range(1, width):
        upper = np.concatenate([[large], accumulated[:-1]])
        middle = accumulated
        lower = np.concatenate([accumulated[1:], [large]])
        choices = np.stack([upper, middle, lower], axis=0)
        choice = np.argmin(choices, axis=0)
        parents[:, x] = choice.astype(np.int8) - 1
        accumulated = cost[:, x] + np.min(choices, axis=0)

    seam = np.empty(width, dtype=np.int32)
    seam[-1] = int(np.argmin(accumulated))
    for x in range(width - 1, 0, -1):
        seam[x - 1] = np.clip(
            seam[x] + int(parents[seam[x], x]), 0, height - 1
        )

    yy = np.arange(height, dtype=np.float32)[:, None]
    alpha = (yy - seam[None, :] + feather) / max(1.0, 2.0 * feather)
    return np.clip(alpha, 0.0, 1.0)[:, :, None]


def blend_overlap(current: np.ndarray, previous: np.ndarray, width: int) -> np.ndarray:
    feather = max(4, width // 160)
    alpha = minimum_cost_seam(current, previous, feather)
    blended = (
        current.astype(np.float32) * (1.0 - alpha)
        + previous.astype(np.float32) * alpha
    )
    return np.clip(blended, 0, 255).astype(np.uint8)


def make_qa_montage(
    mosaic: np.ndarray, boundaries: list[int], destination: Path
) -> None:
    if not boundaries:
        return
    count = min(16, len(boundaries))
    chosen = np.linspace(0, len(boundaries) - 1, count).round().astype(int)
    crop_height = min(150, max(80, mosaic.shape[1] // 5))
    crops = []
    for display_index, boundary_index in enumerate(chosen, start=1):
        center = boundaries[int(boundary_index)]
        top = max(0, center - crop_height // 2)
        bottom = min(mosaic.shape[0], top + crop_height)
        crop = mosaic[top:bottom].copy()
        cv2.putText(
            crop,
            f"seam {int(boundary_index) + 1}",
            (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 0, 0),
            4,
            cv2.LINE_AA,
        )
        cv2.putText(
            crop,
            f"seam {int(boundary_index) + 1}",
            (12, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        crops.append(crop)

    columns = 2
    rows = int(np.ceil(len(crops) / columns))
    cell_height = max(item.shape[0] for item in crops)
    montage = np.full(
        (rows * cell_height, columns * mosaic.shape[1], 3),
        230,
        dtype=np.uint8,
    )
    for index, crop in enumerate(crops):
        row, column = divmod(index, columns)
        y = row * cell_height
        x = column * mosaic.shape[1]
        montage[y : y + crop.shape[0], x : x + crop.shape[1]] = crop
    imwrite_unicode(destination, montage, quality=95)


def build_mosaic(
    frame_paths: list[Path],
    selected: np.ndarray,
    homographies: np.ndarray,
    analysis_displacements: np.ndarray,
    width: int,
    destination: Path,
    qa_destination: Path | None = None,
) -> tuple[int, int]:
    selected_paths = [frame_paths[int(index)] for index in selected]
    first = read_resized(selected_paths[0], width)
    height = first.shape[0]
    scaled_displacements = [
        int(np.clip(round(value * width / ANALYSIS_WIDTH), 1, height - 2))
        for value in analysis_displacements
    ]
    total_height = height + sum(scaled_displacements)
    mosaic = np.empty((total_height, width, 3), dtype=np.uint8)

    current = read_resized(selected_paths[-1], width)
    output_y = 0
    start_row = 0
    boundaries: list[int] = []

    for pair_index in range(len(selected_paths) - 2, -1, -1):
        previous = read_resized(selected_paths[pair_index], width)
        displacement = scaled_displacements[pair_index]
        next_displacement = (
            scaled_displacements[pair_index - 1]
            if pair_index > 0
            else height
        )
        target_overlap = max(48, width // 10)
        overlap = int(
            min(
                target_overlap,
                max(12, displacement * 0.38),
                max(12, next_displacement * 0.38),
                height - displacement - 2,
            )
        )
        homography = scale_homography(homographies[pair_index], width)
        warped = elastic_warp_piece(
            current, homography, displacement, overlap
        )
        warped = color_match(
            warped, previous[:overlap], displacement, overlap
        )

        unique = warped[start_row:displacement]
        mosaic[output_y : output_y + unique.shape[0]] = unique
        output_y += unique.shape[0]
        transition = blend_overlap(
            warped[displacement : displacement + overlap],
            previous[:overlap],
            width,
        )
        boundaries.append(output_y + overlap // 2)
        mosaic[output_y : output_y + overlap] = transition
        output_y += overlap

        current = previous
        start_row = overlap
        if pair_index % 10 == 0 or pair_index == len(selected_paths) - 2:
            print(
                f"融合进度：{len(selected_paths) - 1 - pair_index:02d}/"
                f"{len(selected_paths) - 1:02d}"
            )

    remainder = current[start_row:]
    mosaic[output_y : output_y + remainder.shape[0]] = remainder
    output_y += remainder.shape[0]
    if output_y != total_height:
        raise RuntimeError(
            f"输出高度不一致：实际{output_y}，预计{total_height}"
        )

    imwrite_unicode(destination, mosaic, quality=94)
    if qa_destination is not None:
        make_qa_montage(mosaic, boundaries, qa_destination)
    return width, total_height


def global_rigid_poses(rigid_transforms: np.ndarray) -> np.ndarray:
    poses = [np.eye(3, dtype=np.float64)]
    for transform in rigid_transforms:
        poses.append(poses[-1] @ transform)
    pose_array = np.stack(poses)
    center = np.array(
        [
            ANALYSIS_WIDTH / 2.0,
            (SOURCE_HEIGHT * ANALYSIS_WIDTH / SOURCE_WIDTH) / 2.0,
            1.0,
        ],
        dtype=np.float64,
    )
    start = pose_array[0] @ center
    end = pose_array[-1] @ center
    direction = end[:2] - start[:2]
    route_angle = math.atan2(float(direction[1]), float(direction[0]))
    rotation_angle = -math.pi / 2.0 - route_angle
    cosine, sine = math.cos(rotation_angle), math.sin(rotation_angle)
    common_rotation = np.array(
        [
            [cosine, -sine, 0.0],
            [sine, cosine, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return np.stack([common_rotation @ pose for pose in pose_array])


def elastic_rectify_full(
    image: np.ndarray,
    residual: np.ndarray,
    displacement: int,
) -> np.ndarray:
    height, width = image.shape[:2]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    source_x = xx.copy()
    source_y = yy.copy()
    for _ in range(8):
        denominator = (
            residual[2, 0] * source_x
            + residual[2, 1] * source_y
            + residual[2, 2]
        )
        mapped_x = (
            residual[0, 0] * source_x
            + residual[0, 1] * source_y
            + residual[0, 2]
        ) / denominator
        mapped_y = (
            residual[1, 0] * source_x
            + residual[1, 1] * source_y
            + residual[1, 2]
        ) / denominator
        weight = smoothstep(
            np.clip(source_y / max(1.0, float(displacement)), 0.0, 1.0)
        )
        forward_x = source_x + weight * (mapped_x - source_x)
        forward_y = source_y + weight * (mapped_y - source_y)
        source_x += (xx - forward_x) * 0.9
        source_y += (yy - forward_y) * 0.9
    return cv2.remap(
        image,
        source_x,
        source_y,
        interpolation=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REFLECT_101,
    )


def aligned_previous_in_current(
    previous: np.ndarray, rigid: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    height, width = previous.shape[:2]
    previous_to_current = np.linalg.inv(rigid)
    aligned = cv2.warpPerspective(
        previous,
        previous_to_current,
        (width, height),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REFLECT_101,
    )
    valid = cv2.warpPerspective(
        np.full((height, width), 255, dtype=np.uint8),
        previous_to_current,
        (width, height),
        flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    return aligned, valid


def curved_seam_alpha(
    rectified_current: np.ndarray,
    aligned_previous: np.ndarray,
    previous_valid: np.ndarray,
    displacement: int,
) -> tuple[np.ndarray, np.ndarray]:
    height, width = rectified_current.shape[:2]
    half_band = int(
        min(
            max(40, width // 12),
            max(18, displacement * 0.3),
            max(18, (height - displacement) * 0.3),
        )
    )
    top = max(0, displacement - half_band)
    bottom = min(height, displacement + half_band)
    current_band = rectified_current[top:bottom]
    previous_band = aligned_previous[top:bottom]
    valid_band = previous_valid[top:bottom]

    current_sample = current_band.astype(np.float32)
    previous_sample = previous_band.astype(np.float32)
    valid_pixels = valid_band > 0
    margin = max(4, width // 10)
    sample_mask = valid_pixels.copy()
    sample_mask[:, :margin] = False
    sample_mask[:, width - margin :] = False
    if np.count_nonzero(sample_mask) > 100:
        difference = previous_sample[sample_mask] - current_sample[sample_mask]
        offsets = np.clip(np.median(difference, axis=0), -14.0, 14.0)
    else:
        offsets = np.zeros(3, dtype=np.float32)
    rows = np.arange(height, dtype=np.float32)
    strength = smoothstep(
        np.clip(rows / max(1.0, float(displacement)), 0.0, 1.0)
    )[:, None, None]
    corrected = np.clip(
        rectified_current.astype(np.float32)
        + strength * offsets[None, None, :],
        0,
        255,
    ).astype(np.uint8)
    current_band = corrected[top:bottom]

    previous_weight = minimum_cost_seam(
        current_band, previous_band, max(12, width // 38)
    )
    previous_weight[valid_band[:, :, None] == 0] = 0.0
    alpha = np.ones((height, width), dtype=np.float32)
    alpha[bottom:] = 0.0
    alpha[top:bottom] = 1.0 - previous_weight[:, :, 0]
    return corrected, alpha


def transformed_bounds(
    poses: np.ndarray, width: int, height: int
) -> tuple[np.ndarray, np.ndarray]:
    corners = np.float32(
        [[[0, 0]], [[width, 0]], [[width, height]], [[0, height]]]
    )
    all_points = [
        cv2.perspectiveTransform(corners, pose).reshape(-1, 2)
        for pose in poses
    ]
    points = np.vstack(all_points)
    return points.min(axis=0), points.max(axis=0)


def make_curved_qa_montage(
    mosaic: np.ndarray,
    seam_points: list[tuple[int, int]],
    destination: Path,
) -> None:
    if not seam_points:
        return
    count = min(16, len(seam_points))
    chosen = np.linspace(0, len(seam_points) - 1, count).round().astype(int)
    crop_width = min(560, mosaic.shape[1])
    crop_height = min(260, mosaic.shape[0])
    crops: list[np.ndarray] = []
    for item in chosen:
        x, y = seam_points[int(item)]
        left = int(np.clip(x - crop_width // 2, 0, mosaic.shape[1] - crop_width))
        top = int(
            np.clip(y - crop_height // 2, 0, mosaic.shape[0] - crop_height)
        )
        crop = mosaic[top : top + crop_height, left : left + crop_width].copy()
        label = f"seam {int(item) + 1}"
        cv2.putText(
            crop,
            label,
            (12, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            4,
            cv2.LINE_AA,
        )
        cv2.putText(
            crop,
            label,
            (12, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
        crops.append(crop)
    columns = 2
    rows = int(np.ceil(len(crops) / columns))
    montage = np.full(
        (rows * crop_height, columns * crop_width, 3),
        235,
        dtype=np.uint8,
    )
    for index, crop in enumerate(crops):
        row, column = divmod(index, columns)
        montage[
            row * crop_height : (row + 1) * crop_height,
            column * crop_width : (column + 1) * crop_width,
        ] = crop
    imwrite_unicode(destination, montage, quality=95)


def build_curved_mosaic(
    frame_paths: list[Path],
    selected: np.ndarray,
    homographies: np.ndarray,
    rigid_transforms: np.ndarray,
    analysis_displacements: np.ndarray,
    desired_frame_width: int,
    max_canvas_pixels: int,
    destination: Path,
    qa_destination: Path | None = None,
) -> tuple[int, int, int]:
    selected_paths = [frame_paths[int(index)] for index in selected]
    poses = global_rigid_poses(rigid_transforms)
    analysis_height = int(
        round(SOURCE_HEIGHT * ANALYSIS_WIDTH / SOURCE_WIDTH)
    )
    minimum, maximum = transformed_bounds(
        poses, ANALYSIS_WIDTH, analysis_height
    )
    bounds_size = maximum - minimum
    desired_scale = desired_frame_width / ANALYSIS_WIDTH
    pixel_area_at_analysis = float(bounds_size[0] * bounds_size[1])
    safe_scale = math.sqrt(max_canvas_pixels / max(1.0, pixel_area_at_analysis))
    render_scale = min(desired_scale, safe_scale)
    frame_width = max(320, int(round(ANALYSIS_WIDTH * render_scale)))
    render_scale = frame_width / ANALYSIS_WIDTH
    frame_height = int(round(analysis_height * render_scale))

    margin = max(12, frame_width // 40)
    canvas_width = int(math.ceil(bounds_size[0] * render_scale)) + margin * 2
    canvas_height = int(math.ceil(bounds_size[1] * render_scale)) + margin * 2
    print(
        f"保留弯道画布：{canvas_width}×{canvas_height}，"
        f"单帧宽度：{frame_width}"
    )
    mosaic = np.full((canvas_height, canvas_width, 3), 255, dtype=np.uint8)
    valid_canvas = np.zeros((canvas_height, canvas_width), dtype=np.uint8)

    scale_matrix = np.diag([render_scale, render_scale, 1.0])
    canvas_shift = np.array(
        [
            [1.0, 0.0, -minimum[0] * render_scale + margin],
            [0.0, 1.0, -minimum[1] * render_scale + margin],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    target_poses = np.stack(
        [
            scale_matrix @ pose @ np.linalg.inv(scale_matrix)
            for pose in poses
        ]
    )
    source_edge_weight = frame_edge_weight(frame_height, frame_width)
    source_valid_mask = np.full(
        (frame_height, frame_width),
        255,
        dtype=np.uint8,
    )

    def composite(
        image: np.ndarray,
        alpha: np.ndarray,
        pose: np.ndarray,
    ) -> None:
        corners = np.float32(
            [[[0, 0]], [[frame_width, 0]], [[frame_width, frame_height]],
             [[0, frame_height]]]
        )
        full_transform = canvas_shift @ pose
        warped_corners = cv2.perspectiveTransform(
            corners, full_transform
        ).reshape(-1, 2)
        left = max(0, int(math.floor(warped_corners[:, 0].min())) - 2)
        right = min(
            canvas_width, int(math.ceil(warped_corners[:, 0].max())) + 3
        )
        top = max(0, int(math.floor(warped_corners[:, 1].min())) - 2)
        bottom = min(
            canvas_height, int(math.ceil(warped_corners[:, 1].max())) + 3
        )
        if right <= left or bottom <= top:
            return
        local_shift = np.array(
            [[1.0, 0.0, -left], [0.0, 1.0, -top], [0.0, 0.0, 1.0]]
        )
        local_transform = local_shift @ full_transform
        size = (right - left, bottom - top)
        warped_image = cv2.warpPerspective(
            image,
            local_transform,
            size,
            flags=cv2.INTER_LANCZOS4,
            # Replication avoids introducing synthetic white/black pixels at
            # the source-frame edge. The separately warped weight still
            # limits the valid footprint to the true image.
            borderMode=cv2.BORDER_REPLICATE,
        )
        source_weight = (
            alpha.astype(np.float32) / 255.0
        ) * source_edge_weight
        warped_weight = cv2.warpPerspective(
            source_weight,
            local_transform,
            size,
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        warped_valid = cv2.warpPerspective(
            source_valid_mask,
            local_transform,
            size,
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        ) > 0
        existing = mosaic[top:bottom, left:right]
        existing_valid = valid_canvas[top:bottom, left:right]
        new_pixels = warped_valid & (existing_valid == 0)
        region = new_pixels | (warped_valid & (warped_weight > 0.002))
        if not np.any(region):
            return
        weight = warped_weight
        weight = cv2.GaussianBlur(
            weight,
            (0, 0),
            sigmaX=max(4.0, frame_width / 48.0),
            sigmaY=max(4.0, frame_width / 48.0),
        )
        weight = np.clip(weight, 0.0, 1.0)
        weight[new_pixels] = 1.0
        blended = (
            warped_image.astype(np.float32) * weight[:, :, None]
            + existing.astype(np.float32) * (1.0 - weight[:, :, None])
        )
        existing[region] = np.clip(blended[region], 0, 255).astype(np.uint8)
        existing_valid[region] = 255

    previous = read_resized(selected_paths[0], frame_width)
    composite(
        previous,
        np.full((frame_height, frame_width), 255, dtype=np.uint8),
        target_poses[0],
    )
    seam_points: list[tuple[int, int]] = []

    for index in range(1, len(selected_paths)):
        current = read_resized(selected_paths[index], frame_width)
        homography = scale_homography(homographies[index - 1], frame_width)
        rigid = scale_homography(rigid_transforms[index - 1], frame_width)
        displacement = int(
            np.clip(
                round(analysis_displacements[index - 1] * render_scale),
                1,
                frame_height - 2,
            )
        )
        residual = np.linalg.inv(rigid) @ homography
        rectified = elastic_rectify_full(current, residual, displacement)
        previous_local, previous_valid = aligned_previous_in_current(
            previous, rigid
        )
        corrected, alpha = curved_seam_alpha(
            rectified, previous_local, previous_valid, displacement
        )
        composite(
            corrected,
            np.clip(alpha * 255.0, 0, 255).astype(np.uint8),
            target_poses[index],
        )

        seam_source = np.array(
            [frame_width / 2.0, float(displacement), 1.0],
            dtype=np.float64,
        )
        seam_global = canvas_shift @ target_poses[index] @ seam_source
        seam_global /= seam_global[2]
        seam_points.append(
            (int(round(seam_global[0])), int(round(seam_global[1])))
        )
        previous = current
        if index % 10 == 0 or index == len(selected_paths) - 1:
            print(f"曲线融合进度：{index:02d}/{len(selected_paths) - 1:02d}")

    valid_rows, valid_columns = np.where(valid_canvas > 0)
    top = max(0, int(valid_rows.min()) - 2)
    bottom = min(canvas_height, int(valid_rows.max()) + 3)
    left = max(0, int(valid_columns.min()) - 2)
    right = min(canvas_width, int(valid_columns.max()) + 3)
    mosaic = mosaic[top:bottom, left:right]
    seam_points = [(x - left, y - top) for x, y in seam_points]
    imwrite_unicode(destination, mosaic, quality=94)
    if qa_destination is not None:
        make_curved_qa_montage(mosaic, seam_points, qa_destination)
    return mosaic.shape[1], mosaic.shape[0], frame_width


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["align", "preview", "high", "both"],
        default="preview",
    )
    parser.add_argument("--reuse-alignment", action="store_true")
    args = parser.parse_args()

    frame_paths = sorted(FRAME_DIR.glob("*.jpg"))
    if len(frame_paths) < 2:
        raise RuntimeError("未找到2秒抽帧素材")

    if args.reuse_alignment and NEW_ALIGNMENT.exists():
        selected, homographies, rigid_transforms, displacements = load_alignment()
    else:
        selected, homographies, rigid_transforms, displacements = estimate_all(
            frame_paths
        )

    if args.mode == "align":
        poses = global_rigid_poses(rigid_transforms)
        height = int(round(SOURCE_HEIGHT * ANALYSIS_WIDTH / SOURCE_WIDTH))
        minimum, maximum = transformed_bounds(poses, ANALYSIS_WIDTH, height)
        size = maximum - minimum
        print(
            f"保留实际弯道后的分析画布边界："
            f"{size[0]:.1f}×{size[1]:.1f}"
        )
        return

    if args.mode in {"preview", "both"}:
        path = OUTPUT / "整条道路拼接_优化保留弯道_预览.jpg"
        qa_path = OUTPUT / "优化保留弯道_接缝检查.jpg"
        size = build_curved_mosaic(
            frame_paths,
            selected,
            homographies,
            rigid_transforms,
            displacements,
            desired_frame_width=600,
            max_canvas_pixels=45_000_000,
            destination=path,
            qa_destination=qa_path,
        )
        print(
            f"保留弯道优化预览完成：{path}，"
            f"{size[0]}×{size[1]}，单帧宽度{size[2]}"
        )
        print(f"接缝检查图：{qa_path}")

    if args.mode in {"high", "both"}:
        path = OUTPUT / "整条道路拼接_优化保留弯道_高清.jpg"
        size = build_curved_mosaic(
            frame_paths,
            selected,
            homographies,
            rigid_transforms,
            displacements,
            desired_frame_width=1200,
            max_canvas_pixels=110_000_000,
            destination=path,
        )
        print(
            f"保留弯道优化高清完成：{path}，"
            f"{size[0]}×{size[1]}，单帧宽度{size[2]}"
        )


if __name__ == "__main__":
    main()
