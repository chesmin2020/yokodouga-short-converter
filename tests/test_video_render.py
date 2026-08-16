from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.video_render import (
    _split_caption,
    choose_montage_clips,
    _kept_ranges,
    _post_metadata,
    subtitle_events_for_range,
    write_ass,
    video_filter,
)


def candidates() -> list[dict]:
    return [
        {"start": 0, "end": 40, "duration": 40, "score": 90},
        {"start": 50, "end": 95, "duration": 45, "score": 85},
        {"start": 110, "end": 155, "duration": 45, "score": 80},
    ]


def segments() -> list[dict]:
    return [
        {"start": value, "end": value + 5, "text": "実はここが一番驚いたポイントです。"}
        for value in range(0, 160, 5)
    ]


class VideoRenderTest(unittest.TestCase):
    def test_long_japanese_caption_is_split_and_retimed(self) -> None:
        pieces = _split_caption("実際に使ってみて一番驚いたのは画面がとても見やすいことです。")
        self.assertGreater(len(pieces), 1)
        events = subtitle_events_for_range(
            [{"start": 10, "end": 14, "text": "実際に使って驚きました。"}], 11, 14
        )
        self.assertEqual(events[0]["start"], 0)
        self.assertEqual(events[-1]["end"], 3)

    def test_ass_file_contains_vertical_video_style(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "captions.ass"
            write_ass(path, [{"start": 0, "end": 2, "text": "大きく読みやすい日本語字幕です"}])
            content = path.read_text(encoding="utf-8")
            self.assertIn("PlayResY: 1920", content)
            self.assertIn(r"\N", content)

    def test_ass_file_contains_custom_title_size_and_position(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "captions.ass"
            write_ass(
                path, [{"start": 0, "end": 2, "text": "編集した字幕"}],
                title="製品レビュー", duration=3, subtitle_size=88,
                subtitle_position="middle", title_size=64, title_margin_top=180,
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("Style: Default,Hiragino Sans,88", content)
            self.assertIn(",5,70,70,0,1", content)
            self.assertIn("製品レビュー", content)
            self.assertIn("Style: Title,Hiragino Sans,64", content)
            self.assertIn(",8,70,70,180,1", content)

    def test_manual_line_breaks_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "line-breaks.ass"
            events = subtitle_events_for_range(
                [{"start": 0, "end": 2, "text": "字幕の一行目\n字幕の二行目"}], 0, 2
            )
            write_ass(path, events, title="タイトル一行目\nタイトル二行目", duration=2)
            content = path.read_text(encoding="utf-8")
            self.assertIn(r"字幕の一行目\N字幕の二行目", content)
            self.assertIn(r"タイトル一行目\Nタイトル二行目", content)

    def test_spaces_in_telop_are_preserved(self) -> None:
        events = subtitle_events_for_range(
            [{"start": 0, "end": 2, "text": "この スペースを　残す"}], 0, 2
        )
        self.assertIn(" ", events[0]["text"])
        self.assertIn("　", events[0]["text"])

    def test_ending_telop_is_shown_only_at_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ending.ass"
            write_ass(path, [], duration=10, ending_text="続きは本編で！", ending_size=120, ending_duration=2.5)
            content = path.read_text(encoding="utf-8")
            self.assertIn("Style: Ending,Hiragino Sans,120", content)
            self.assertIn("Dialogue: 2,0:00:07.50,0:00:10.00,Ending", content)

    def test_montage_chooses_non_overlapping_short_clips(self) -> None:
        clips = choose_montage_clips(candidates(), segments())
        self.assertEqual(len(clips), 3)
        for clip in clips:
            self.assertGreaterEqual(clip["end"] - clip["start"], 8)
            self.assertLessEqual(clip["end"] - clip["start"], 15)

    def test_deleted_transcript_interval_is_removed_from_video_ranges(self) -> None:
        ranges = _kept_ranges(0, 15, segments(), [1])
        self.assertEqual(ranges, [{"start": 0, "end": 5.0}, {"start": 10.0, "end": 15}])

    def test_horizontal_crop_positions(self) -> None:
        self.assertIn("crop=1080:1920:0:0", video_filter("left"))
        self.assertIn("crop=1080:1920:(iw-ow)/2:0", video_filter("center"))
        self.assertIn("crop=1080:1920:iw-ow:0", video_filter("right"))

    def test_blurred_background_preserves_the_complete_source_frame(self) -> None:
        filter_graph = video_filter("blur")
        self.assertIn("split=2[background][foreground]", filter_graph)
        self.assertIn("boxblur=20:2", filter_graph)
        self.assertIn("force_original_aspect_ratio=decrease", filter_graph)
        self.assertIn("overlay=(W-w)/2:(H-h)/2", filter_graph)

    def test_post_title_and_description_are_generated(self) -> None:
        metadata = _post_metadata(
            "驚きの新機能", [{
                "summary": "便利な機能を紹介", "hook": "これは知っておきたい",
                "transcript_excerpt": "実際に使いました。特に操作が簡単でした。",
            }], source_title="元動画のタイトル",
            source_description="製品を詳しくレビューした動画です。\n初心者向けに解説します。",
            source_url="https://youtu.be/abcdefghijk", source_channel="テストチャンネル",
        )
        self.assertIn("驚きの新機能", metadata["post_title"])
        self.assertIn("便利な機能を紹介", metadata["description"])
        self.assertIn("#Shorts", metadata["description"])
        self.assertIn("製品を詳しくレビューした動画です。", metadata["description"])
        self.assertIn("https://youtu.be/abcdefghijk", metadata["description"])

    def test_render_endpoint_queues_selected_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = Path(temp)
            job_id = "abc789abc789"
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "status": "candidates_analyzed", "analysis": {"candidates": candidates()}}),
                encoding="utf-8",
            )
            with patch.object(main, "JOBS_DIR", jobs), patch.object(main.executor, "submit") as submit:
                response = TestClient(main.app).post(
                    f"/api/jobs/{job_id}/render",
                    json={"mode": "montage", "candidate_indices": [0, 2]},
                )
            self.assertEqual(response.status_code, 202)
            submit.assert_called_once_with(
                main.run_video_render, job_id, "montage", [0, 2], True,
                {}, "見どころまとめ", 72, "lower", 52, 90, {},
                "続きは本編で！", 96, 2.5, {}, {}, [],
            )

    def test_render_endpoint_rejects_single_montage_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = Path(temp)
            job_id = "789abc789abc"
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "status": "candidates_analyzed", "analysis": {"candidates": candidates()}}),
                encoding="utf-8",
            )
            with patch.object(main, "JOBS_DIR", jobs):
                response = TestClient(main.app).post(
                    f"/api/jobs/{job_id}/render",
                    json={"mode": "montage", "candidate_indices": [0]},
                )
            self.assertEqual(response.status_code, 400)

    def test_delete_output_removes_file_and_job_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = root / "jobs"; jobs.mkdir()
            outputs = root / "outputs"; outputs.mkdir()
            job_id = "de1e7ede1e7e"
            filename = f"{job_id}_short_01.mp4"
            (outputs / filename).write_bytes(b"video")
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "status": "videos_rendered", "outputs": [
                    {"filename": filename, "type": "individual", "duration": 10}
                ]}), encoding="utf-8",
            )
            with patch.object(main, "JOBS_DIR", jobs), patch.object(main, "OUTPUT_DIR", outputs):
                response = TestClient(main.app).delete(f"/api/jobs/{job_id}/outputs/{filename}")
            self.assertEqual(response.status_code, 200)
            self.assertFalse((outputs / filename).exists())
            saved = json.loads((jobs / f"{job_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["outputs"], [])

    def test_source_video_endpoint_serves_only_the_jobs_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = root / "jobs"; jobs.mkdir()
            uploads = root / "uploads"; uploads.mkdir()
            job_id = "fee123fee123"
            filename = f"{job_id}.mp4"
            (uploads / filename).write_bytes(b"preview-video")
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "stored_name": filename}), encoding="utf-8"
            )
            with patch.object(main, "JOBS_DIR", jobs), patch.object(main, "UPLOAD_DIR", uploads):
                response = TestClient(main.app).get(f"/api/jobs/{job_id}/source")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "video/mp4")
            self.assertEqual(response.content, b"preview-video")

    def test_source_video_endpoint_rejects_missing_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = root / "jobs"; jobs.mkdir()
            uploads = root / "uploads"; uploads.mkdir()
            job_id = "bad123bad123"
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "stored_name": "missing.mp4"}), encoding="utf-8"
            )
            with patch.object(main, "JOBS_DIR", jobs), patch.object(main, "UPLOAD_DIR", uploads):
                response = TestClient(main.app).get(f"/api/jobs/{job_id}/source")
            self.assertEqual(response.status_code, 404)

    def test_worker_records_finished_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = root / "jobs"; jobs.mkdir()
            uploads = root / "uploads"; uploads.mkdir()
            outputs = root / "outputs"; outputs.mkdir()
            job_id = "fed123fed123"
            job = {
                "job_id": job_id, "stored_name": f"{job_id}.mp4", "status": "rendering_video",
                "analysis": {"candidates": candidates()}, "transcript": {"segments": segments()},
            }
            (jobs / f"{job_id}.json").write_text(json.dumps(job), encoding="utf-8")
            expected = [{"filename": f"{job_id}_montage.mp4", "type": "montage", "duration": 30}]
            with (
                patch.object(main, "JOBS_DIR", jobs), patch.object(main, "UPLOAD_DIR", uploads),
                patch.object(main, "OUTPUT_DIR", outputs), patch.object(main, "render_montage", return_value=expected),
            ):
                main.run_video_render(
                    job_id, "montage", [0, 1], True, {}, "見どころまとめ",
                    72, "lower", 52, 90, {}, "続きは本編で！", 96, 2.5,
                    {}, {}, [],
                )
            saved = json.loads((jobs / f"{job_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "videos_rendered")
            self.assertEqual(saved["outputs"], expected)


if __name__ == "__main__":
    unittest.main()
