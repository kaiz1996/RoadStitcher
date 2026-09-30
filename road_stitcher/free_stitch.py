from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .cv_compat import load_cv2, load_numpy
from .media import imread_unicode, imwrite_unicode
from .state import PipelineControl


ANALYSIS_WIDTH = 1000
FREE_ALIGNMENT_VERSION = "free-quality-v3"


@dataclass(slots=True)
class PairAlignment:
    previous_index: int
    current_index: int
    matches: int
    inliers: int
    reprojection_error: float
    overlap: float
    scale: float
    angle_degrees: float
    displacement: float


@dataclass(slots=True)
class FreeAlignment:
    selected: object
    poses: object
    pair_metrics: list[PairAlignment]
    analysis_width: int
    analysis_height: int


def _read_resized(path: Path, width: int):
    cv2 = load_cv2()
    image = imread_unicode(path)
    height = max(1, int(round(image.shape[0] * width / image.shape[1])))
    interpolation = (
        cv2.INTER_AREA if image.shape[1] > width else cv2.INTER_LANCZOS4
    )
    return cv2.resize(image, (width, height), interpolation=interpolation)


def _feature_mask(height: int, width: int):
    cv2 = load_cv2()
    np = load_numpy()
    mask = np.zeros((height, width), dtype=np.uint8)
    margin_x = max(8, int(round(width * 0.035)))
    margin_y = max(8, int(round(height * 0.045)))
    mask[margin_y : height - margin_y, margin_x : width - margin_x] = 255
    # Softening the hard mask edge avoids concentrating keypoints on a crop.
    return cv2.GaussianBlur(mask, (0, 0), sigmaX=4.0)


def _features(image):
    cv2 = load_cv2()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    detector = cv2.SIFT_create(
        nfeatures=6500,
        contrastThreshold=0.018,
        edgeThreshold=12,
        sigma=1.4,
    )
    return detector.detectAndCompute(
        gray,
        _feature_mask(gray.shape[0], gray.shape[1]),
    )


def _overlap_ratio(transform, width: int, height: int) -> float:
    cv2 = load_cv2()
    np = load_numpy()
    corners = np.float32(
        [[0, 0], [width, 0], [width, height], [0, height]]
    ).reshape(-1, 1, 2)
    warped = cv2.perspectiveTransform(corners, transform).reshape(-1, 2)
    reference = np.float32(
        [[0, 0], [width, 0], [width, height], [0, height]]
    )
    try:
        area, _ = cv2.intersectConvexConvex(
            warped.astype(np.float32),
            reference,
        )
    except cv2.error:
        return 0.0
    return float(max(0.0, min(1.0, area / float(width * height))))


def _transform_properties(transform, width: int, height: int):
    np = load_numpy()
    linear = transform[:2, :2]
    scale = float(
        math.sqrt(max(1e-12, abs(float(np.linalg.det(linear)))))
    )
    angle = math.degrees(math.atan2(transform[1, 0], transform[0, 0]))
    center = np.array([width / 2.0, height / 2.0, 1.0])
    moved = transform @ center
    moved /= moved[2]
    displacement = float(np.linalg.norm(moved[:2] - center[:2]))
    return scale, angle, displacement


def _match_features(
    previous_features,
    current_features,
    width: int,
    height: int,
    previous_index: int,
    current_index: int,
):
    cv2 = load_cv2()
    np = load_numpy()
    keypoints_previous, descriptors_previous = previous_features
    keypoints_current, descriptors_current = current_features
    if descriptors_previous is None or descriptors_current is None:
        raise RuntimeError(
            f"第{previous_index + 1}与第{current_index + 1}张画面"
            "缺少可匹配纹理"
        )

    matcher = cv2.BFMatcher(cv2.NORM_L2)
    pairs = matcher.knnMatch(
        descriptors_current,
        descriptors_previous,
        k=2,
    )
    good = [
        first
        for first, second in pairs
        if first.distance < 0.74 * second.distance
    ]
    if len(good) < 24:
        raise RuntimeError(
            f"第{previous_index + 1}与第{current_index + 1}张画面"
            f"有效匹配不足（{len(good)}个），请缩短抽帧间隔"
        )

    current_points = np.float32(
        [keypoints_current[item.queryIdx].pt for item in good]
    ).reshape(-1, 1, 2)
    previous_points = np.float32(
        [keypoints_previous[item.trainIdx].pt for item in good]
    ).reshape(-1, 1, 2)
    affine, inlier_mask = cv2.estimateAffinePartial2D(
        current_points,
        previous_points,
        method=cv2.RANSAC,
        ransacReprojThreshold=3.2,
        maxIters=7000,
        confidence=0.999,
        refineIters=30,
    )
    if affine is None or inlier_mask is None:
        raise RuntimeError(
            f"第{previous_index + 1}与第{current_index + 1}张画面"
            "无法估算稳定移动关系"
        )

    inliers = inlier_mask.reshape(-1).astype(bool)
    inlier_count = int(inliers.sum())
    if inlier_count < 18 or inlier_count / len(good) < 0.18:
        raise RuntimeError(
            f"第{previous_index + 1}与第{current_index + 1}张画面"
            f"配准可信度不足（{inlier_count}/{len(good)}）"
        )

    transform = np.vstack(
        [affine.astype(np.float64), [0.0, 0.0, 1.0]]
    )
    scale, angle, displacement = _transform_properties(
        transform,
        width,
        height,
    )
    if not 0.82 <= scale <= 1.22:
        raise RuntimeError(
            f"第{previous_index + 1}与第{current_index + 1}张画面"
            f"比例变化过大（{scale:.3f}），可能处于升降阶段"
        )
    if abs(angle) > 35.0:
        raise RuntimeError(
            f"第{previous_index + 1}与第{current_index + 1}张画面"
            f"航向突变过大（{angle:.1f}°）"
        )

    predicted = cv2.transform(current_points, affine)
    residuals = np.linalg.norm(
        predicted.reshape(-1, 2) - previous_points.reshape(-1, 2),
        axis=1,
    )
    error = float(np.median(residuals[inliers]))
    if error > 3.0:
        raise RuntimeError(
            f"第{previous_index + 1}与第{current_index + 1}张画面"
            f"对齐误差过大（{error:.2f}px），请缩短抽帧间隔"
        )
    overlap = _overlap_ratio(transform, width, height)
    if overlap < 0.22:
        raise RuntimeError(
            f"第{previous_index + 1}与第{current_index + 1}张画面"
            f"重叠不足（{overlap:.0%}），请缩短抽帧间隔"
        )

    metrics = PairAlignment(
        previous_index=previous_index,
        current_index=current_index,
        matches=len(good),
        inliers=inlier_count,
        reprojection_error=error,
        overlap=overlap,
        scale=scale,
        angle_degrees=angle,
        displacement=displacement,
    )
    return transform, metrics


def _pose_center(pose, width: int, height: int):
    np = load_numpy()
    point = pose @ np.array([width / 2.0, height / 2.0, 1.0])
    point /= point[2]
    return point[:2]


def _pose_angle(pose) -> float:
    return math.degrees(math.atan2(pose[1, 0], pose[0, 0]))


def _select_keyframes(poses, width: int, height: int):
    np = load_numpy()
    count = len(poses)
    if count <= 2:
        return np.arange(count, dtype=np.int32)

    # Keep enough overlap for a clean handoff, but do not repeatedly blend
    # nearly identical frames. Dense frame averaging turns small, normal
    # registration residuals into visible ghosting on markings and fences.
    threshold = min(width, height) * 0.36
    selected = [0]
    last = 0
    for index in range(1, count - 1):
        movement = float(
            np.linalg.norm(
                _pose_center(poses[index], width, height)
                - _pose_center(poses[last], width, height)
            )
        )
        rotation = abs(_pose_angle(poses[index]) - _pose_angle(poses[last]))
        if movement >= threshold or rotation >= 5.0:
            selected.append(index)
            last = index

    final_movement = float(
        np.linalg.norm(
            _pose_center(poses[-1], width, height)
            - _pose_center(poses[selected[-1]], width, height)
        )
    )
    if final_movement >= threshold * 0.35 or selected[-1] == 0:
        selected.append(count - 1)
    elif selected[-1] != count - 1:
        selected[-1] = count - 1
    return np.asarray(sorted(set(selected)), dtype=np.int32)


def align_free_translation(
    frame_paths: list[Path],
    cache_path: Path,
    report_path: Path,
    control: PipelineControl,
    log: Callable[[str], None],
    progress: Callable[[float, str], None] | None = None,
) -> FreeAlignment:
    cv2 = load_cv2()
    np = load_numpy()
    if len(frame_paths) < 2:
        raise RuntimeError("自由平移拼接至少需要两张画面")

    first = _read_resized(frame_paths[0], ANALYSIS_WIDTH)
    height, width = first.shape[:2]
    dense_poses = [np.eye(3, dtype=np.float64)]
    metrics: list[PairAlignment] = []
    previous_features = _features(first)

    for index in range(1, len(frame_paths)):
        control.checkpoint()
        current = _read_resized(frame_paths[index], width)
        current_features = _features(current)
        transform, item = _match_features(
            previous_features,
            current_features,
            width,
            height,
            index - 1,
            index,
        )
        dense_poses.append(dense_poses[-1] @ transform)
        metrics.append(item)
        log(
            f"{index:03d}->{index + 1:03d} "
            f"匹配={item.matches} 内点={item.inliers} "
            f"误差={item.reprojection_error:.2f}px "
            f"重叠={item.overlap:.0%} "
            f"位移={item.displacement:.1f}px "
            f"旋转={item.angle_degrees:.2f}°"
        )
        previous_features = current_features
        if progress:
            progress(
                index / max(1, len(frame_paths) - 1),
                f"全画面配准 {index + 1}/{len(frame_paths)}",
            )

    dense_poses_array = np.stack(dense_poses)
    selected = _select_keyframes(dense_poses_array, width, height)
    selected_poses = dense_poses_array[selected]
    log(f"关键帧筛选：{len(frame_paths)}张 → {len(selected)}张")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cache_path,
        selected=selected,
        poses=selected_poses,
        analysis_width=np.int32(width),
        analysis_height=np.int32(height),
        source_count=np.int32(len(frame_paths)),
        engine_version=np.array(FREE_ALIGNMENT_VERSION),
    )
    report = {
        "engine": FREE_ALIGNMENT_VERSION,
        "analysis_width": width,
        "analysis_height": height,
        "source_frames": len(frame_paths),
        "selected_frames": selected.tolist(),
        "pairs": [asdict(item) for item in metrics],
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return FreeAlignment(
        selected=selected,
        poses=selected_poses,
        pair_metrics=metrics,
        analysis_width=width,
        analysis_height=height,
    )


def load_free_alignment(
    cache_path: Path,
    frame_count: int,
) -> FreeAlignment | None:
    np = load_numpy()
    if not cache_path.is_file():
        return None
    try:
        data = np.load(cache_path, allow_pickle=False)
        if int(data["source_count"]) != frame_count:
            return None
        if str(data["engine_version"].item()) != FREE_ALIGNMENT_VERSION:
            return None
        return FreeAlignment(
            selected=data["selected"].astype(np.int32),
            poses=data["poses"].astype(np.float64),
            pair_metrics=[],
            analysis_width=int(data["analysis_width"]),
            analysis_height=int(data["analysis_height"]),
        )
    except (OSError, KeyError, ValueError):
        return None


def _scaled_pose(pose, scale: float):
    np = load_numpy()
    scaling = np.diag([scale, scale, 1.0])
    return scaling @ pose @ np.linalg.inv(scaling)


def _transformed_bounds(poses, width: int, height: int):
    cv2 = load_cv2()
    np = load_numpy()
    corners = np.float32(
        [[[0, 0]], [[width, 0]], [[width, height]], [[0, height]]]
    )
    points = [
        cv2.perspectiveTransform(corners, pose).reshape(-1, 2)
        for pose in poses
    ]
    stacked = np.vstack(points)
    return stacked.min(axis=0), stacked.max(axis=0)


def _center_weight(height: int, width: int):
    cv2 = load_cv2()
    np = load_numpy()
    mask = np.full((height, width), 255, dtype=np.uint8)
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    # distanceTransform on a full mask has no exterior zero; explicitly use
    # distance to all four frame borders.
    yy, xx = np.indices((height, width), dtype=np.float32)
    distance = np.minimum.reduce(
        [xx + 1.0, width - xx, yy + 1.0, height - yy]
    )
    distance /= max(1.0, float(distance.max()))
    return np.clip(distance**0.72, 0.015, 1.0).astype(np.float32)


def _sharp_seam_choice(
    existing_image,
    existing_valid,
    existing_weight,
    incoming_image,
    incoming_valid,
    incoming_weight,
):
    """Choose one source per pixel along a low-detail overlap seam.

    Dynamic-programming seam finding keeps markings and fences from being
    double-exposed while avoiding the rectangular seam left by a pure
    centre-distance handoff. It is deliberately a mask operation: source
    pixels are never averaged.
    """
    cv2 = load_cv2()
    np = load_numpy()
    overlap = existing_valid & incoming_valid
    fallback = incoming_valid & (
        (~existing_valid) | (incoming_weight >= existing_weight)
    )
    if int(np.count_nonzero(overlap)) < 800:
        return fallback
    try:
        masks = [
            np.ascontiguousarray(existing_valid.astype(np.uint8) * 255),
            np.ascontiguousarray(incoming_valid.astype(np.uint8) * 255),
        ]
        seam_masks = cv2.detail_DpSeamFinder("COLOR_GRAD").find(
            [
                np.ascontiguousarray(existing_image.astype(np.float32)),
                np.ascontiguousarray(incoming_image.astype(np.float32)),
            ],
            [(0, 0), (0, 0)],
            masks,
        )
        incoming_mask = seam_masks[1].get() > 0
        choice = incoming_mask & incoming_valid
        # The seam finder occasionally keeps the complete incoming mask on
        # low-texture water or asphalt. Keep this valid (and sharp) result.
        return choice
    except cv2.error:
        return fallback


def render_free_mosaic(
    frame_paths: list[Path],
    alignment: FreeAlignment,
    desired_frame_width: int,
    max_canvas_pixels: int,
    destination: Path,
    control: PipelineControl,
    log: Callable[[str], None],
    progress: Callable[[float, str], None] | None = None,
) -> tuple[int, int, int]:
    cv2 = load_cv2()
    np = load_numpy()
    selected_paths = [
        frame_paths[int(index)] for index in alignment.selected
    ]
    poses = alignment.poses
    minimum, maximum = _transformed_bounds(
        poses,
        alignment.analysis_width,
        alignment.analysis_height,
    )
    extent = maximum - minimum
    desired_scale = desired_frame_width / alignment.analysis_width
    safe_scale = math.sqrt(
        max_canvas_pixels / max(1.0, float(extent[0] * extent[1]))
    )
    render_scale = min(desired_scale, safe_scale)
    frame_width = max(
        160,
        int(round(alignment.analysis_width * render_scale)),
    )
    render_scale = frame_width / alignment.analysis_width
    frame_height = int(
        round(alignment.analysis_height * render_scale)
    )
    margin = max(10, frame_width // 45)
    canvas_width = int(math.ceil(extent[0] * render_scale)) + margin * 2
    canvas_height = int(math.ceil(extent[1] * render_scale)) + margin * 2
    if canvas_width * canvas_height > max_canvas_pixels * 1.08:
        raise RuntimeError("预计全景画布超过安全限制，请降低输出质量")

    log(
        f"自由平移画布：{canvas_width}×{canvas_height}，"
        f"关键帧：{len(selected_paths)}，单帧宽度：{frame_width}"
    )
    mosaic = np.full(
        (canvas_height, canvas_width, 3),
        255,
        dtype=np.uint8,
    )
    confidence = np.zeros(
        (canvas_height, canvas_width),
        dtype=np.uint16,
    )
    shift = np.array(
        [
            [1.0, 0.0, -minimum[0] * render_scale + margin],
            [0.0, 1.0, -minimum[1] * render_scale + margin],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    render_poses = [
        _scaled_pose(pose, render_scale) for pose in poses
    ]
    base_weight = _center_weight(frame_height, frame_width)
    source_corners = np.float32(
        [[[0, 0]], [[frame_width, 0]], [[frame_width, frame_height]],
         [[0, frame_height]]]
    )

    for index, (path, pose) in enumerate(
        zip(selected_paths, render_poses, strict=True)
    ):
        control.checkpoint()
        image = _read_resized(path, frame_width)
        full_transform = shift @ pose
        corners = cv2.perspectiveTransform(
            source_corners,
            full_transform,
        ).reshape(-1, 2)
        left = max(0, int(math.floor(corners[:, 0].min())) - 2)
        right = min(
            canvas_width,
            int(math.ceil(corners[:, 0].max())) + 3,
        )
        top = max(0, int(math.floor(corners[:, 1].min())) - 2)
        bottom = min(
            canvas_height,
            int(math.ceil(corners[:, 1].max())) + 3,
        )
        if right <= left or bottom <= top:
            continue
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
            borderMode=cv2.BORDER_REPLICATE,
        )
        warped_weight = cv2.warpPerspective(
            base_weight,
            local_transform,
            size,
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        new_valid = warped_weight > 0.002
        if not np.any(new_valid):
            continue

        region = mosaic[top:bottom, left:right]
        old_confidence_u16 = confidence[top:bottom, left:right]
        old_weight = old_confidence_u16.astype(np.float32) / 65535.0
        overlap = new_valid & (old_confidence_u16 > 0)

        if np.count_nonzero(overlap) >= 800:
            stable = overlap & (warped_weight > 0.18) & (old_weight > 0.18)
            if np.count_nonzero(stable) >= 400:
                difference = (
                    region[stable].astype(np.float32)
                    - warped_image[stable].astype(np.float32)
                )
                offset = np.clip(
                    np.median(difference, axis=0),
                    -18.0,
                    18.0,
                )
                warped_image = np.clip(
                    warped_image.astype(np.float32) + offset,
                    0,
                    255,
                ).astype(np.uint8)

        # A full weighted average across wide overlaps is forgiving of small
        # exposure changes, but it also averages 1–2 px alignment residuals
        # into a double image. Keep the sharper source whose pixel is closer
        # to its frame centre; blend only in the narrow handoff where both
        # sources have essentially equal authority.
        # Never average source pixels here. The seam finder selects the least
        # disruptive path through the overlap, then one source owns each pixel.
        take_new = _sharp_seam_choice(
            region,
            old_confidence_u16 > 0,
            old_weight,
            warped_image,
            new_valid,
            warped_weight,
        )
        region[take_new] = warped_image[take_new]
        combined = np.maximum(old_weight, warped_weight)
        old_confidence_u16[new_valid] = np.clip(
            combined[new_valid] * 65535.0,
            1,
            65535,
        ).astype(np.uint16)

        if progress:
            progress(
                (index + 1) / len(selected_paths),
                f"融合关键帧 {index + 1}/{len(selected_paths)}",
            )

    valid_rows, valid_columns = np.where(confidence > 0)
    if len(valid_rows) == 0:
        raise RuntimeError("自由平移拼接未生成有效画面")
    top = max(0, int(valid_rows.min()) - 2)
    bottom = min(canvas_height, int(valid_rows.max()) + 3)
    left = max(0, int(valid_columns.min()) - 2)
    right = min(canvas_width, int(valid_columns.max()) + 3)
    mosaic = mosaic[top:bottom, left:right]
    imwrite_unicode(destination, mosaic, quality=94)
    return mosaic.shape[1], mosaic.shape[0], frame_width
