from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main


class TranscriptionJobTest(unittest.TestCase):
    def test_ui_keeps_transcript_internal_without_result_list(self) -> None:
        html = (main.BASE_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("slice(0, 100)", html)
        self.assertIn("currentTranscript = data", html)
        self.assertNotIn("<h2>文字起こし結果</h2>", html)

    def test_start_endpoint_queues_background_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = Path(temp)
            job_id = "aaaabbbbcccc"
            (jobs / f"{job_id}.json").write_text(
                json.dumps({
                    "job_id": job_id,
                    "stored_name": f"{job_id}.mp4",
                    "status": "uploaded",
                }),
                encoding="utf-8",
            )
            with (
                patch.object(main, "JOBS_DIR", jobs),
                patch.object(main.executor, "submit") as submit,
            ):
                response = TestClient(main.app).post(
                    f"/api/jobs/{job_id}/transcribe"
                )

            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["status"], "queued_for_transcription")
            submit.assert_called_once_with(main.run_transcription, job_id)

    def test_run_transcription_saves_segments_and_completes_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploads = root / "uploads"
            jobs = root / "jobs"
            transcripts = root / "transcripts"
            for directory in (uploads, jobs, transcripts):
                directory.mkdir()
            job_id = "abcdef123456"
            (uploads / f"{job_id}.mp4").write_bytes(b"media")
            (jobs / f"{job_id}.json").write_text(
                json.dumps({
                    "job_id": job_id,
                    "stored_name": f"{job_id}.mp4",
                    "status": "queued_for_transcription",
                }),
                encoding="utf-8",
            )
            result = {
                "language": "ja",
                "segments": [{"start": 1.2, "end": 3.4, "text": "テストです"}],
                "text": "テストです",
            }
            with (
                patch.object(main, "UPLOAD_DIR", uploads),
                patch.object(main, "JOBS_DIR", jobs),
                patch.object(main, "TRANSCRIPTS_DIR", transcripts),
                patch.object(main, "transcribe_media", return_value=result),
            ):
                main.run_transcription(job_id)

            job = json.loads((jobs / f"{job_id}.json").read_text(encoding="utf-8"))
            saved = json.loads(
                (transcripts / f"{job_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(job["status"], "transcribed")
            self.assertEqual(job["next_step"], "candidate_analysis")
            self.assertEqual(saved["segments"][0]["start"], 1.2)

    def test_run_transcription_records_readable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            uploads = root / "uploads"
            jobs = root / "jobs"
            transcripts = root / "transcripts"
            for directory in (uploads, jobs, transcripts):
                directory.mkdir()
            job_id = "123456abcdef"
            (uploads / f"{job_id}.mp4").write_bytes(b"media")
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "stored_name": f"{job_id}.mp4"}),
                encoding="utf-8",
            )
            with (
                patch.object(main, "UPLOAD_DIR", uploads),
                patch.object(main, "JOBS_DIR", jobs),
                patch.object(main, "TRANSCRIPTS_DIR", transcripts),
                patch.object(main, "transcribe_media", side_effect=RuntimeError("モデル読込失敗")),
            ):
                main.run_transcription(job_id)

            job = json.loads((jobs / f"{job_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(job["status"], "transcription_failed")
            self.assertEqual(job["error"], "モデル読込失敗")


if __name__ == "__main__":
    unittest.main()
