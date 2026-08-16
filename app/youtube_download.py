from __future__ import annotations

from pathlib import Path
import re
import shutil
import time
from typing import Any, Callable
from urllib.parse import urlparse

ProgressCallback = Callable[[float, str], None]

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def validate_youtube_url(url: str) -> str:
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in YOUTUBE_HOSTS:
        raise ValueError("YouTubeの動画URLを入力してください")
    return cleaned


def download_youtube_video(
    url: str, job_id: str, output_dir: Path, progress: ProgressCallback
) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "yt-dlpがインストールされていません。"
            "起動スクリプトを再実行してください。"
        ) from exc

    cleaned_url = validate_youtube_url(url)

    def hook(event: dict[str, Any]) -> None:
        if event.get("status") == "downloading":
            downloaded = float(event.get("downloaded_bytes") or 0)
            total = float(event.get("total_bytes") or event.get("total_bytes_estimate") or 0)
            percent = downloaded / total * 85 if total else 10
            progress(min(90, max(5, percent)), "YouTubeから動画をダウンロード中")
        elif event.get("status") == "finished":
            progress(92, "映像と音声を結合中")

    options = {
        "outtmpl": str(output_dir / f"{job_id}.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "source_address": "0.0.0.0",
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
    }
    node = shutil.which("node")
    if node:
        options["js_runtimes"] = {"node": {"path": node}}
    # Separate high-resolution YouTube streams can return 403 even when the
    # progressive MP4 for the same video is available. Prefer the reliable
    # audio+video stream because this app re-encodes the source afterwards.
    formats = ["18/b[ext=mp4]/best"] * 3
    info = None
    last_error: Exception | None = None
    for attempt, format_selector in enumerate(formats):
        attempt_options = {**options, "format": format_selector}
        if attempt:
            progress(5, f"YouTubeへの接続を再試行中（{attempt + 1}/3）")
            time.sleep(attempt * 2)
            for partial in output_dir.glob(f"{job_id}.*"):
                if partial.name.endswith((".part", ".ytdl")):
                    partial.unlink(missing_ok=True)
        try:
            with yt_dlp.YoutubeDL(attempt_options) as downloader:
                info = downloader.extract_info(cleaned_url, download=True)
            break
        except Exception as exc:
            last_error = exc
    if info is None:
        message = str(last_error).strip() if last_error else "不明なエラー"
        message = re.sub(r"\x1b\[[0-9;]*m", "", message)
        if "HTTP Error 403" in message:
            message += "。YouTubeをブラウザで開けるか確認し、VPNを切ってから再試行してください"
        raise RuntimeError(f"YouTube動画の読み込みに失敗しました: {message}") from last_error

    files = [
        path for path in output_dir.glob(f"{job_id}.*")
        if path.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
    ]
    if not files:
        raise RuntimeError("YouTube動画の保存ファイルを確認できませんでした")
    media_path = max(files, key=lambda path: path.stat().st_size)
    return {
        "stored_name": media_path.name,
        "original_name": str(info.get("title") or "YouTube動画"),
        "youtube_description": str(info.get("description") or "")[:12000],
        "youtube_channel": str(info.get("channel") or info.get("uploader") or "")[:200],
        "youtube_tags": [str(tag)[:100] for tag in (info.get("tags") or [])[:20]],
        "youtube_id": info.get("id"),
        "youtube_url": cleaned_url,
        "duration": info.get("duration"),
    }
