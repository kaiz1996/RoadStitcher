from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Callable

from .cv_compat import load_cv2, load_numpy
from .media import VideoInfo
from .state import PipelineControl


def interval_is_reliable(
    overlaps: list[float],
    anchor_count: int,
) -> bool:
    if len(overlaps) < 3 or anchor_count <= 0:
        return False
    median_overlap = float(median(overlaps))
    success_ratio = len(overlaps) / anchor_count
    return median_overlap >= 0.42 and success_ratio >= 0.75


def _read_frame(capture, timestamp: float, width: int):
    cv2 = load_cv2()
    capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
    ok, frame = capture.read()
    if not ok:
        return None
    height = int(round(frame.shape[0] * width / frame.shape[1]))
    return cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)


def _estimate_overlap(
    previous,
    current,
    road_left_ratio: float,
    road_right_ratio: float,
) -> float | None:
    cv2 = load_cv2()
    np = load_numpy()
    previous_gray = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)
    current_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    height, width = previous_gray.shape
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[
        :,
        int(width * road_left_ratio) : int(width * road_right_ratio),
    ] = 255
    detector = cv2.SIFT_create(nfeatures=3500, contrastThreshold=0.02)
    kp_previous, desc_previous = detector.detectAndCompute(previous_gray, mask)
    kp_current, desc_current = detector.detectAndCompute(current_gray, mask)
    if desc_previous is None or desc_current is None:
        return None
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    pairs = matcher.knnMatch(desc_current, desc_previous, k=2)
    good = [
        first
        for first, second in pairs
        if first.distance < 0.75 * second.distance
    ]
    if len(good) < 18:
        return None
    current_points = np.float32(
        [kp_current[item.queryIdx].pt for item in good]
    ).reshape(-1, 1, 2)
    previous_points = np.float32(
        [kp_previous[item.trainIdx].pt for item in good]
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
    if affine is None or inliers is None or int(inliers.sum()) < 12:
        return None
    transform = np.vstack([affine, [0.0, 0.0, 1.0]])
    corners = np.float32(
        [[[0, 0]], [[width, 0]], [[width, height]], [[0, height]]]
    )
    warped = cv2.perspectiveTransform(corners, transform).reshape(-1, 2)
    original = np.float32(
        [[0, 0], [width, 0], [width, height], [0, height]]
    )
    area, _ = cv2.intersectConvexConvex(
        warped.astype(np.float32),
        original,
    )
    return max(0.0, min(1.0, float(area) / float(width * height)))


def recommend_interval(
    video_path: Path,
    info: VideoInfo,
    road_left_ratio: float,
    road_right_ratio: float,
    control: PipelineControl,
    progress: Callable[[float, str], None] | None = None,
) -> tuple[float, dict[float, list[float]]]:
    cv2 = load_cv2()
    candidates = [5.0, 4.0, 3.0, 2.0, 1.5, 1.0, 0.5]
    anchors = [
        max(0.0, info.duration_seconds * ratio)
        for ratio in (0.18, 0.38, 0.58, 0.78)
    ]
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{video_path}")
    results: dict[float, list[float]] = {}
    try:
        total = len(candidates) * len(anchors)
        completed = 0
        for candidate in candidates:
            overlaps: list[float] = []
            for anchor in anchors:
                control.checkpoint()
                completed += 1
                if anchor + candidate >= info.duration_seconds:
                    continue
                previous = _read_frame(capture, anchor, 800)
                current = _read_frame(capture, anchor + candidate, 800)
                if previous is None or current is None:
                    continue
                overlap = _estimate_overlap(
                    previous,
                    current,
                    road_left_ratio,
                    road_right_ratio,
                )
                if overlap is not None:
                    overlaps.append(overlap)
                if progress:
                    progress(
                        completed / total,
                        f"测试抽帧间隔 {candidate:g} 秒",
                    )
            results[candidate] = overlaps
            if interval_is_reliable(overlaps, len(anchors)):
                return candidate, results
    finally:
        capture.release()
    return 1.0, results
