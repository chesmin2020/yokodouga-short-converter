from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.candidate_analysis import analyze_locally, analyze_transcript


def sample_transcript() -> dict:
    segments = []
    for index in range(24):
        start = index * 4.0
        text = (
            "実は比較して一番驚いたポイントは画質の違いです。"
            if 8 <= index <= 17
            else "実際に使った結果を順番に説明します。"
        )
        segments.append({"start": start, "end": start + 4.0, "text": text})
    return {"duration": 96.0, "segments": segments}


class CandidateAnalysisTest(unittest.TestCase):
    def test_local_analysis_returns_ranked_non_overlapping_candidates(self) -> None:
        candidates = analyze_locally(sample_transcript()["segments"])
        self.assertGreaterEqual(len(candidates), 2)
        self.assertGreaterEqual(candidates[0]["score"], candidates[-1]["score"])
        for candidate in candidates:
            self.assertIn("summary", candidate)
            self.assertIn("reason", candidate)
            self.assertIn("hook", candidate)
            self.assertGreater(candidate["end"], candidate["start"])
            self.assertGreaterEqual(candidate["duration"], 10)
            self.assertLessEqual(candidate["duration"], 90)

    def test_local_analysis_has_no_fixed_ten_candidate_limit(self) -> None:
        segments = [
            {
                "start": index * 4.0,
                "end": index * 4.0 + 4.0,
                "text": f"実は比較して驚いたポイント{index}です。",
            }
            for index in range(240)
        ]
        self.assertGreater(len(analyze_locally(segments)), 10)

    def test_analysis_uses_local_engine_without_api_key(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            result = analyze_transcript(sample_transcript(), lambda *_: None)
        self.assertEqual(result["engine"], "local")
        self.assertTrue(result["candidates"])

    def test_analysis_can_be_limited_to_a_requested_range(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            result = analyze_transcript(
                sample_transcript(), lambda *_: None, range_start=20, range_end=68
            )
        self.assertEqual(result["analysis_range"], {"start": 20.0, "end": 68.0})
        self.assertTrue(result["candidates"])
        self.assertTrue(all(item["start"] >= 20 for item in result["candidates"]))
        self.assertTrue(all(item["end"] <= 68 for item in result["candidates"]))

    def test_worker_saves_candidate_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = root / "jobs"; jobs.mkdir()
            transcripts = root / "transcripts"; transcripts.mkdir()
            candidates = root / "candidates"; candidates.mkdir()
            job_id = "abc123abc123"
            job = {
                "job_id": job_id,
                "stored_name": f"{job_id}.mp4",
                "status": "queued_for_analysis",
                "transcript": sample_transcript(),
            }
            (jobs / f"{job_id}.json").write_text(json.dumps(job), encoding="utf-8")
            with (
                patch.object(main, "JOBS_DIR", jobs),
                patch.object(main, "TRANSCRIPTS_DIR", transcripts),
                patch.object(main, "CANDIDATES_DIR", candidates),
                patch.dict("os.environ", {"OPENAI_API_KEY": ""}),
            ):
                main.run_candidate_analysis(job_id)
            saved_job = json.loads((jobs / f"{job_id}.json").read_text(encoding="utf-8"))
            saved = json.loads((candidates / f"{job_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_job["status"], "candidates_analyzed")
            self.assertEqual(saved_job["next_step"], "video_generation")
            self.assertEqual(saved["engine"], "local")

    def test_endpoint_queues_candidate_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = Path(temp)
            job_id = "def456def456"
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "status": "transcribed"}),
                encoding="utf-8",
            )
            with (
                patch.object(main, "JOBS_DIR", jobs),
                patch.object(main.executor, "submit") as submit,
            ):
                response = TestClient(main.app).post(
                    f"/api/jobs/{job_id}/analyze-candidates"
                )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["status"], "queued_for_analysis")
            submit.assert_called_once_with(main.run_candidate_analysis, job_id, None, None)

    def test_endpoint_queues_requested_analysis_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = Path(temp)
            job_id = "abc456abc456"
            (jobs / f"{job_id}.json").write_text(
                json.dumps({
                    "job_id": job_id,
                    "status": "transcribed",
                    "transcript": sample_transcript(),
                }),
                encoding="utf-8",
            )
            with (
                patch.object(main, "JOBS_DIR", jobs),
                patch.object(main.executor, "submit") as submit,
            ):
                response = TestClient(main.app).post(
                    f"/api/jobs/{job_id}/analyze-candidates",
                    json={"range_start": 20, "range_end": 68},
                )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(
                response.json()["analysis_request"],
                {"range_start": 20.0, "range_end": 68.0},
            )
            submit.assert_called_once_with(main.run_candidate_analysis, job_id, 20.0, 68.0)

    def test_endpoint_rejects_analysis_range_shorter_than_ten_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = Path(temp)
            job_id = "bad456bad456"
            (jobs / f"{job_id}.json").write_text(
                json.dumps({
                    "job_id": job_id,
                    "status": "transcribed",
                    "transcript": sample_transcript(),
                }),
                encoding="utf-8",
            )
            with patch.object(main, "JOBS_DIR", jobs):
                response = TestClient(main.app).post(
                    f"/api/jobs/{job_id}/analyze-candidates",
                    json={"range_start": 20, "range_end": 25},
                )
            self.assertEqual(response.status_code, 400)
            self.assertIn("10秒以上", response.json()["detail"])

    def test_ui_contains_analysis_scope_and_incremental_candidate_controls(self) -> None:
        response = TestClient(main.app).get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="full"', response.text)
        self.assertIn('value="range"', response.text)
        self.assertIn('id="analysisStart"', response.text)
        self.assertIn('id="analysisEnd"', response.text)
        self.assertIn('id="showMoreBtn"', response.text)
        self.assertIn("slice(startIndex, startIndex + 30)", response.text)


if __name__ == "__main__":
    unittest.main()
