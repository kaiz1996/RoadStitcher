from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from road_stitcher.config import JobConfig, WORK_DIR_NAME
from road_stitcher.extractor import expected_frame_count
from road_stitcher.interval import interval_is_reliable
from road_stitcher.media import VideoInfo
from road_stitcher.pipeline import (
    free_render_signature,
    road_render_signature,
    safe_name,
)
from road_stitcher.state import (
    JobStage,
    JobStore,
    PipelineCancelled,
    PipelineControl,
)
from road_stitcher.ui import (
    clear_processing_cache,
    default_output_dir_for_video,
    format_duration,
    format_file_size,
    normalize_windows_path_text,
)
from optimize_aerial_road_stitch import (
    ROAD_ALIGNMENT_VERSION,
    alignment_is_current,
    frame_edge_weight,
)


class ConfigTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "sample.mp4"
            video.write_bytes(b"placeholder")
            config = JobConfig(
                video_path=video,
                output_dir=root / "output",
                project_name="测试道路",
                stitch_mode="free",
            )
            config.validate()
            restored = JobConfig.from_dict(config.to_dict())
            self.assertEqual(restored.video_path, video.resolve())
            self.assertEqual(restored.project_name, "测试道路")
            self.assertEqual(restored.stitch_mode, "free")

    def test_invalid_interval_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "sample.mp4"
            video.write_bytes(b"placeholder")
            config = JobConfig(
                video_path=video,
                output_dir=root / "output",
                interval_seconds=0.1,
            )
            with self.assertRaises(ValueError):
                config.validate()

    def test_processing_files_are_kept_in_a_separate_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "sample.mp4"
            video.write_bytes(b"placeholder")
            config = JobConfig(video_path=video, output_dir=root / "output")
            config.validate()
            self.assertEqual(config.work_dir, config.output_dir / WORK_DIR_NAME)
            self.assertEqual(config.job_file.parent, config.work_dir)
            self.assertEqual(config.log_file.parent, config.work_dir)
            self.assertEqual(config.frames_dir(2.5).parent, config.work_dir)

    def test_reset_cache_preserves_finished_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "output"
            cache = root / WORK_DIR_NAME
            cache.mkdir(parents=True)
            (cache / "alignment.npz").write_bytes(b"cache")
            result = root / "road_preview.jpg"
            result.write_bytes(b"result")

            clear_processing_cache(root)

            self.assertFalse(cache.exists())
            self.assertTrue(result.is_file())


class StateTests(unittest.TestCase):
    def test_state_is_saved_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "sample.mp4"
            video.write_bytes(b"placeholder")
            config = JobConfig(video_path=video, output_dir=root / "output")
            config.validate()
            store = JobStore(config)
            store.update(
                stage=JobStage.EXTRACTING,
                progress=0.25,
                message="抽帧中",
            )
            data = json.loads(config.job_file.read_text(encoding="utf-8"))
            self.assertEqual(data["stage"], JobStage.EXTRACTING)
            self.assertEqual(data["progress"], 0.25)
            self.assertEqual(data["message"], "抽帧中")

    def test_state_save_retries_a_temporary_windows_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "sample.mp4"
            video.write_bytes(b"placeholder")
            config = JobConfig(video_path=video, output_dir=root / "output")
            store = JobStore(config)
            real_replace = __import__("os").replace
            calls = 0

            def flaky_replace(source, destination) -> None:
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise PermissionError("temporary test lock")
                real_replace(source, destination)

            with mock.patch(
                "road_stitcher.state.os.replace",
                side_effect=flaky_replace,
            ):
                store.update(message="完成")
            self.assertEqual(calls, 3)
            self.assertTrue(config.job_file.is_file())

    def test_paused_checkpoint_resumes(self) -> None:
        control = PipelineControl()
        control.pause()
        finished = threading.Event()

        def worker() -> None:
            control.checkpoint()
            finished.set()

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.25)
        self.assertFalse(finished.is_set())
        control.resume()
        thread.join(timeout=1.0)
        self.assertTrue(finished.is_set())

    def test_cancelled_checkpoint_raises(self) -> None:
        control = PipelineControl()
        control.cancel()
        with self.assertRaises(PipelineCancelled):
            control.checkpoint()


class UtilityTests(unittest.TestCase):
    def test_safe_name(self) -> None:
        self.assertEqual(safe_name('A:B/C*D?'), "A_B_C_D_")

    def test_interval_uses_true_even_sample_median(self) -> None:
        overlaps = [0.2935, 0.5124, 0.9987, 0.2574]
        self.assertFalse(interval_is_reliable(overlaps, 4))

    def test_expected_frame_count(self) -> None:
        info = VideoInfo(
            width=3840,
            height=2160,
            fps=50.0,
            frame_count=8807,
            duration_seconds=176.14,
        )
        self.assertEqual(expected_frame_count(info, 2.0), 89)
        self.assertEqual(expected_frame_count(info, 5.0), 36)

    def test_windows_path_text_uses_backslashes(self) -> None:
        self.assertEqual(
            normalize_windows_path_text("C:/Users/DELL/Videos/T1.mp4"),
            r"C:\Users\DELL\Videos\T1.mp4",
        )

    def test_default_output_is_beside_the_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "T1.mp4"
            expected = video.parent / "T1_拼接结果"
            self.assertEqual(default_output_dir_for_video(video), expected)

    def test_video_summary_formatting_is_compact(self) -> None:
        self.assertEqual(format_duration(176.14), "02:56")
        self.assertEqual(format_duration(3723.0), "1:02:03")
        self.assertEqual(format_file_size(3_758_096_384), "3.5 GB")

    def test_road_frame_weight_fades_every_source_edge(self) -> None:
        weight = frame_edge_weight(240, 400)
        self.assertEqual(weight.shape, (240, 400))
        self.assertTrue((weight[0, :] == 0).all())
        self.assertTrue((weight[-1, :] == 0).all())
        self.assertTrue((weight[:, 0] == 0).all())
        self.assertTrue((weight[:, -1] == 0).all())
        self.assertGreater(float(weight[120, 200]), 0.99)

    def test_road_render_signature_tracks_quality_settings(self) -> None:
        preview = road_render_signature(600, 45_000_000)
        high = road_render_signature(1200, 110_000_000)
        self.assertNotEqual(preview, high)
        self.assertEqual(preview["renderer"], "road-edge-feather-v2")

    def test_free_render_signature_tracks_original_route_renderer(self) -> None:
        signature = free_render_signature(1200, 110_000_000)
        self.assertEqual(signature["renderer"], "free-dp-seam-native-v6")

    def test_road_alignment_cache_requires_the_current_engine(self) -> None:
        np = __import__("numpy")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "precise_alignment.npz"
            np.savez_compressed(path, engine_version=np.array(ROAD_ALIGNMENT_VERSION))
            self.assertTrue(alignment_is_current(path))
            np.savez_compressed(path, engine_version=np.array("old-engine"))
            self.assertFalse(alignment_is_current(path))

    def test_road_alignment_splits_an_oversized_sparse_pair(self) -> None:
        import optimize_aerial_road_stitch as optimizer

        np = __import__("numpy")
        identity = np.eye(3, dtype=np.float64)
        fake_image = np.zeros((500, 1000, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                optimizer, "OUTPUT", root
            ), mock.patch.object(
                optimizer, "NEW_ALIGNMENT", root / "precise_alignment.npz"
            ), mock.patch.object(
                optimizer, "select_frames", return_value=np.array([0, 4])
            ), mock.patch.object(
                optimizer, "read_resized", return_value=fake_image
            ), mock.patch.object(
                optimizer,
                "estimate_homography",
                return_value=(identity, identity, 100, 80, 0.5),
            ), mock.patch.object(
                optimizer,
                "center_displacement",
                side_effect=[100.0, 600.0, 100.0, 100.0],
            ):
                selected, _, _, displacements = optimizer.estimate_all(
                    [root / f"frame_{index}.jpg" for index in range(5)]
                )

            self.assertEqual(selected.tolist(), [0, 2, 4])
            self.assertEqual(displacements.tolist(), [100.0, 100.0])


if __name__ == "__main__":
    unittest.main()
