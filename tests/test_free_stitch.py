from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from road_stitcher.cv_compat import load_cv2, load_numpy
from road_stitcher.free_stitch import (
    FREE_ALIGNMENT_VERSION,
    align_free_translation,
    load_free_alignment,
    render_free_mosaic,
)
from road_stitcher.media import imwrite_unicode
from road_stitcher.state import PipelineControl


class FreeTranslationTests(unittest.TestCase):
    def test_curved_translation_sequence_is_aligned_and_rendered(self) -> None:
        cv2 = load_cv2()
        np = load_numpy()
        rng = np.random.default_rng(20260730)
        base = rng.integers(
            20,
            75,
            size=(1120, 1180, 3),
            dtype=np.uint8,
        )
        for index in range(260):
            center = (
                int(rng.integers(15, base.shape[1] - 15)),
                int(rng.integers(15, base.shape[0] - 15)),
            )
            radius = int(rng.integers(3, 13))
            color = tuple(
                int(value) for value in rng.integers(90, 245, size=3)
            )
            cv2.circle(base, center, radius, color, -1, cv2.LINE_AA)
        for value in range(80, 1100, 120):
            cv2.line(
                base,
                (0, value),
                (base.shape[1] - 1, value),
                (160, 120, 80),
                2,
            )
        for value in range(70, 1150, 140):
            cv2.line(
                base,
                (value, 0),
                (value, base.shape[0] - 1),
                (75, 145, 180),
                2,
            )

        positions = [
            (100, 80),
            (110, 160),
            (130, 240),
            (170, 320),
            (220, 400),
            (290, 480),
            (370, 560),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame_paths: list[Path] = []
            for index, (left, top) in enumerate(positions):
                frame = base[top : top + 320, left : left + 480]
                path = root / f"frame_{index:03d}.jpg"
                imwrite_unicode(path, frame, quality=96)
                frame_paths.append(path)

            cache = root / "alignment.npz"
            report = root / "alignment.json"
            alignment = align_free_translation(
                frame_paths,
                cache,
                report,
                PipelineControl(),
                lambda _: None,
            )
            self.assertGreaterEqual(len(alignment.selected), 4)
            self.assertTrue(cache.is_file())
            self.assertTrue(report.is_file())

            restored = load_free_alignment(cache, len(frame_paths))
            self.assertIsNotNone(restored)
            with np.load(cache, allow_pickle=False) as cache_data:
                self.assertEqual(
                    str(cache_data["engine_version"].item()),
                    FREE_ALIGNMENT_VERSION,
                )
            destination = root / "mosaic.jpg"
            width, height, _ = render_free_mosaic(
                frame_paths,
                restored,
                480,
                8_000_000,
                destination,
                PipelineControl(),
                lambda _: None,
            )
            self.assertTrue(destination.is_file())
            self.assertGreater(width, 600)
            self.assertGreater(height, 700)


if __name__ == "__main__":
    unittest.main()
