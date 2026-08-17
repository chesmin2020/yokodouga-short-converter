from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.youtube_upload import oauth_config


class YouTubeUploadTest(unittest.TestCase):
    def test_oauth_config_requires_both_credentials(self) -> None:
        with patch.dict("os.environ", {"YOUTUBE_CLIENT_ID": "", "YOUTUBE_CLIENT_SECRET": ""}):
            self.assertIsNone(oauth_config())
        with patch.dict(
            "os.environ",
            {"YOUTUBE_CLIENT_ID": "client-id", "YOUTUBE_CLIENT_SECRET": "client-secret"},
        ):
            config = oauth_config()
        self.assertEqual(config["web"]["client_id"], "client-id")

    def test_upload_endpoint_queues_private_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = root / "jobs"; jobs.mkdir()
            outputs = root / "outputs"; outputs.mkdir()
            job_id = "abc123def456"
            filename = f"{job_id}_short_01.mp4"
            (outputs / filename).write_bytes(b"video")
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "outputs": [{"filename": filename}]}),
                encoding="utf-8",
            )
            with (
                patch.object(main, "JOBS_DIR", jobs),
                patch.object(main, "OUTPUT_DIR", outputs),
                patch.object(main, "load_credentials", return_value=object()),
                patch.object(main.executor, "submit") as submit,
            ):
                response = TestClient(main.app).post(
                    f"/api/jobs/{job_id}/youtube-upload/{filename}",
                    json={"title": "Shortsタイトル", "description": "概要欄", "privacy_status": "private", "channel_id": "UCtest"},
                )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["status"], "queued")
            submit.assert_called_once_with(
                main.run_youtube_upload, job_id, filename, "Shortsタイトル", "概要欄", "private", "UCtest"
            )

    def test_upload_endpoint_rejects_unconnected_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = Path(temp)
            job_id = "abc123abc123"
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "outputs": []}), encoding="utf-8"
            )
            with patch.object(main, "JOBS_DIR", jobs), patch.object(main, "load_credentials", return_value=None):
                response = TestClient(main.app).post(
                    f"/api/jobs/{job_id}/youtube-upload/video.mp4",
                    json={"title": "test", "channel_id": "UCtest"},
                )
            self.assertEqual(response.status_code, 401)

    def test_worker_records_uploaded_video_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = root / "jobs"; jobs.mkdir()
            outputs = root / "outputs"; outputs.mkdir()
            job_id = "fed456fed456"
            filename = f"{job_id}_short_01.mp4"
            (outputs / filename).write_bytes(b"video")
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id}), encoding="utf-8"
            )
            with (
                patch.object(main, "JOBS_DIR", jobs),
                patch.object(main, "OUTPUT_DIR", outputs),
                patch.object(main, "DATA_DIR", root),
                patch.object(
                    main, "upload_video", return_value={"video_id": "youtube123", "url": "https://youtu.be/youtube123"}
                ),
            ):
                main.run_youtube_upload(job_id, filename, "title", "description", "private", "UCtest")
            saved = json.loads((jobs / f"{job_id}.json").read_text(encoding="utf-8"))
            record = saved["youtube_uploads"][filename]
            self.assertEqual(record["status"], "uploaded")
            self.assertEqual(record["url"], "https://youtu.be/youtube123")


if __name__ == "__main__":
    unittest.main()
