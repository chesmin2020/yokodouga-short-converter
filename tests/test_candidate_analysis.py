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
        self.assertLessEqual(len(candidates), 8)
        self.assertGreaterEqual(candidates[0]["score"], candidates[-1]["score"])
        for candidate in candidates:
            self.assertIn("summary", candidate)
            self.assertIn("reason", candidate)
            self.assertIn("hook", candidate)
            self.assertGreater(candidate["end"], candidate["start"])

    def test_analysis_uses_local_engine_without_api_key(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            result = analyze_transcript(sample_transcript(), lambda *_: None)
        self.assertEqual(result["engine"], "local")
        self.assertTrue(result["candidates"])

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
            submit.assert_called_once_with(main.run_candidate_analysis, job_id)


if __name__ == "__main__":
    unittest.main()
