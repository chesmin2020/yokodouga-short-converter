from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.youtube_download import download_youtube_video, validate_youtube_url


class YouTubeDownloadTest(unittest.TestCase):
    def test_only_youtube_urls_are_accepted(self) -> None:
        self.assertEqual(validate_youtube_url("https://youtu.be/abcdefghijk"), "https://youtu.be/abcdefghijk")
        with self.assertRaises(ValueError):
            validate_youtube_url("https://example.com/video")

    def test_endpoint_creates_job_and_queues_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            jobs = Path(temp)
            with patch.object(main, "JOBS_DIR", jobs), patch.object(main.executor, "submit") as submit:
                response = TestClient(main.app).post(
                    "/api/youtube", json={"url": "https://www.youtube.com/watch?v=abcdefghijk"}
                )
            self.assertEqual(response.status_code, 202)
            data = response.json()
            self.assertEqual(data["status"], "queued_for_youtube_download")
            submit.assert_called_once_with(main.run_youtube_download, data["job_id"], data["youtube_url"])

    def test_403_retries_progressive_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            options_seen = []

            class FakeDownloader:
                def __init__(self, options):
                    self.options = options
                    options_seen.append(options)

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def extract_info(self, _url, download):
                    self.assert_download = download
                    if len(options_seen) == 1:
                        raise RuntimeError("HTTP Error 403: Forbidden")
                    (output / "fallback.mp4").write_bytes(b"video")
                    return {"id": "abcdefghijk", "title": "動画", "description": "元の概要欄"}

            with (
                patch("yt_dlp.YoutubeDL", FakeDownloader),
                patch("app.youtube_download.shutil.which", return_value="/usr/local/bin/node"),
                patch("app.youtube_download.time.sleep"),
            ):
                result = download_youtube_video(
                    "https://youtu.be/abcdefghijk", "fallback", output, lambda *_: None
                )
            self.assertEqual(options_seen[1]["format"], "18/b[ext=mp4]/best")
            self.assertEqual(options_seen[0]["format"], "18/b[ext=mp4]/best")
            self.assertEqual(options_seen[0]["source_address"], "0.0.0.0")
            self.assertEqual(result["youtube_description"], "元の概要欄")

    def test_worker_updates_downloaded_video_as_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = root / "jobs"; jobs.mkdir()
            uploads = root / "uploads"; uploads.mkdir()
            job_id = "123abc123abc"
            (jobs / f"{job_id}.json").write_text(
                json.dumps({"job_id": job_id, "status": "queued_for_youtube_download"}), encoding="utf-8"
            )
            media = uploads / f"{job_id}.mp4"; media.write_bytes(b"video")
            downloaded = {
                "stored_name": media.name, "original_name": "自分のYouTube動画",
                "youtube_id": "abcdefghijk", "youtube_url": "https://youtu.be/abcdefghijk", "duration": 120,
            }
            with (
                patch.object(main, "JOBS_DIR", jobs), patch.object(main, "UPLOAD_DIR", uploads),
                patch.object(main, "download_youtube_video", return_value=downloaded),
                patch.object(main, "media_probe", return_value={"available": True}),
            ):
                main.run_youtube_download(job_id, downloaded["youtube_url"])
            job = json.loads((jobs / f"{job_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(job["status"], "uploaded")
            self.assertEqual(job["next_step"], "transcription")


if __name__ == "__main__":
    unittest.main()
