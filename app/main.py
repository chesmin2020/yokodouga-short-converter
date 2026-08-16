from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from app.candidate_analysis import analyze_transcript
from app.transcription import transcribe_media
from app.youtube_download import download_youtube_video, validate_youtube_url
from app.video_render import _post_metadata, render_individual, render_montage

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
JOBS_DIR = DATA_DIR / "jobs"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
CANDIDATES_DIR = DATA_DIR / "candidates"
for p in (UPLOAD_DIR, OUTPUT_DIR, JOBS_DIR, TRANSCRIPTS_DIR, CANDIDATES_DIR):
    p.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="横動画ショート変換", version="0.6.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcription")
job_lock = threading.Lock()


def job_path(job_id: str) -> Path:
    if not job_id or any(c not in "0123456789abcdef" for c in job_id):
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    return JOBS_DIR / f"{job_id}.json"


def read_job(job_id: str) -> dict[str, Any]:
    path = job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    return json.loads(path.read_text(encoding="utf-8"))


def write_job(job: dict[str, Any]) -> None:
    path = job_path(job["job_id"])
    temporary = path.with_suffix(".json.tmp")
    with job_lock:
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def update_job(job_id: str, **changes: Any) -> dict[str, Any]:
    with job_lock:
        path = job_path(job_id)
        job = json.loads(path.read_text(encoding="utf-8"))
        job.update(changes)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    return job


def run_transcription(job_id: str) -> None:
    try:
        job = read_job(job_id)
        media_path = UPLOAD_DIR / job["stored_name"]

        def progress(percent: float, message: str) -> None:
            update_job(
                job_id,
                status="transcribing",
                progress={"percent": round(percent, 1), "message": message},
            )

        transcript = transcribe_media(media_path, progress)
        transcript_path = TRANSCRIPTS_DIR / f"{job_id}.json"
        transcript_path.write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        update_job(
            job_id,
            status="transcribed",
            progress={"percent": 100, "message": "文字起こしが完了しました"},
            transcript=transcript,
            transcript_file=transcript_path.name,
            next_step="candidate_analysis",
            error=None,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="transcription_failed",
            progress={"percent": 0, "message": "文字起こしに失敗しました"},
            error=str(exc),
        )


def run_candidate_analysis(job_id: str) -> None:
    try:
        job = read_job(job_id)
        transcript = job.get("transcript")
        if not transcript:
            transcript_path = TRANSCRIPTS_DIR / f"{job_id}.json"
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))

        def progress(percent: float, message: str) -> None:
            update_job(
                job_id,
                status="analyzing_candidates",
                progress={"percent": round(percent, 1), "message": message},
            )

        analysis = analyze_transcript(transcript, progress)
        candidates_path = CANDIDATES_DIR / f"{job_id}.json"
        candidates_path.write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        engine_label = "OpenAI" if analysis["engine"] == "openai" else "ローカル評価"
        update_job(
            job_id,
            status="candidates_analyzed",
            progress={"percent": 100, "message": f"{engine_label}で候補解析が完了しました"},
            analysis=analysis,
            candidates_file=candidates_path.name,
            next_step="video_generation",
            error=None,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="candidate_analysis_failed",
            progress={"percent": 0, "message": "候補解析に失敗しました"},
            error=str(exc),
        )


def run_youtube_download(job_id: str, url: str) -> None:
    try:
        def progress(percent: float, message: str) -> None:
            update_job(
                job_id,
                status="downloading_youtube",
                progress={"percent": round(percent, 1), "message": message},
            )

        downloaded = download_youtube_video(url, job_id, UPLOAD_DIR, progress)
        media_path = UPLOAD_DIR / downloaded["stored_name"]
        update_job(
            job_id,
            **downloaded,
            source="youtube",
            status="uploaded",
            probe=media_probe(media_path),
            progress={"percent": 100, "message": "YouTube動画の読み込みが完了しました"},
            next_step="transcription",
            error=None,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="youtube_download_failed",
            progress={"percent": 0, "message": "YouTube動画の読み込みに失敗しました"},
            error=str(exc),
        )


def run_video_render(
    job_id: str,
    mode: str,
    candidate_indices: list[int],
    include_subtitles: bool,
    titles: dict[int, str],
    montage_title: str,
    subtitle_size: int,
    subtitle_position: str,
    title_size: int,
    title_margin_top: int,
    horizontal_positions: dict[int, str],
    ending_text: str,
    ending_size: int,
    ending_duration: float,
    subtitle_overrides: dict[int, str],
    candidate_ranges: dict[int, dict[str, float]],
    excluded_segment_indices: list[int],
) -> None:
    try:
        job = read_job(job_id)
        all_candidates = job["analysis"]["candidates"]
        candidates = []
        media_duration = float(job.get("transcript", {}).get("duration", 0) or 0)
        for index in candidate_indices:
            candidate = dict(all_candidates[index])
            edited_range = candidate_ranges.get(index, {})
            start = max(0.0, float(edited_range.get("start", candidate["start"])))
            end = float(edited_range.get("end", candidate["end"]))
            if media_duration:
                end = min(media_duration, end)
            if end <= start:
                raise ValueError("候補の終了位置は開始位置より後にしてください")
            candidate.update(start=round(start, 3), end=round(end, 3), duration=round(end - start, 3))
            candidates.append(candidate)
        source = UPLOAD_DIR / job["stored_name"]
        raw_segments = job.get("transcript", {}).get("segments", [])
        segments = [
            {**segment, "text": subtitle_overrides.get(index, segment.get("text", ""))}
            for index, segment in enumerate(raw_segments)
        ]
        for candidate in candidates:
            candidate["transcript_excerpt"] = " ".join(
                str(segment.get("text", "")).strip()
                for segment in segments
                if float(segment["end"]) > float(candidate["start"])
                and float(segment["start"]) < float(candidate["end"])
                and str(segment.get("text", "")).strip()
            )[:1200]
        selected_titles = [
            titles.get(index, str(all_candidates[index].get("summary", "")))
            for index in candidate_indices
        ]
        selected_positions = [
            horizontal_positions.get(index, "center") for index in candidate_indices
        ]

        def progress(percent: float, message: str) -> None:
            update_job(
                job_id,
                status="rendering_video",
                progress={"percent": round(percent, 1), "message": message},
            )

        if mode == "individual":
            outputs = render_individual(
                source, OUTPUT_DIR, job_id, candidates, segments, include_subtitles,
                selected_titles, subtitle_size, subtitle_position,
                title_size, title_margin_top, selected_positions,
                ending_text, ending_size, ending_duration,
                excluded_segment_indices, progress,
            )
        else:
            outputs = render_montage(
                source, OUTPUT_DIR, job_id, candidates, segments, include_subtitles,
                montage_title, subtitle_size, subtitle_position,
                title_size, title_margin_top, selected_positions,
                ending_text, ending_size, ending_duration,
                excluded_segment_indices, progress,
            )
        source_metadata = {
            "source_title": str(job.get("original_name", "")),
            "source_description": str(job.get("youtube_description", "")),
            "source_url": str(job.get("youtube_url", "")),
            "source_channel": str(job.get("youtube_channel", "")),
        }
        if mode == "individual":
            for output, candidate, title in zip(outputs, candidates, selected_titles):
                output.update(_post_metadata(title, [candidate], **source_metadata))
        else:
            for output in outputs:
                output.update(_post_metadata(montage_title, candidates, montage=True, **source_metadata))
        cache_key = str(time.time_ns())
        for output in outputs:
            output["cache_key"] = cache_key
        previous = job.get("outputs", [])
        names = {output["filename"] for output in outputs}
        combined = [output for output in previous if output.get("filename") not in names] + outputs
        update_job(
            job_id,
            status="videos_rendered",
            progress={"percent": 100, "message": "動画の書き出しが完了しました"},
            outputs=combined,
            next_step="review_outputs",
            error=None,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="video_render_failed",
            progress={"percent": 0, "message": "動画の書き出しに失敗しました"},
            error=str(exc),
        )


def tool_exists(name: str) -> bool:
    return shutil.which(name) is not None


def media_probe(path: Path) -> dict[str, Any]:
    if not tool_exists("ffprobe"):
        return {"available": False, "reason": "ffprobe not found"}
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size,format_name:stream=index,codec_type,codec_name,width,height,r_frame_rate",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return {"available": True, "error": result.stderr.strip()}
    try:
        return {"available": True, "probe": json.loads(result.stdout)}
    except json.JSONDecodeError:
        return {"available": True, "error": "ffprobe returned invalid JSON"}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (BASE_DIR / "app" / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "ffmpeg": tool_exists("ffmpeg"),
        "ffprobe": tool_exists("ffprobe"),
        "platform": os.name,
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="ファイル名を取得できませんでした")

    suffix = Path(file.filename).suffix.lower()
    allowed = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail=f"未対応形式です: {suffix}")

    job_id = uuid.uuid4().hex[:12]
    safe_name = f"{job_id}{suffix}"
    dest = UPLOAD_DIR / safe_name

    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    job = {
        "job_id": job_id,
        "original_name": file.filename,
        "stored_name": safe_name,
        "status": "uploaded",
        "probe": media_probe(dest),
        "next_step": "transcription",
    }
    write_job(job)
    return job


class YouTubeRequest(BaseModel):
    url: str


@app.post("/api/youtube", status_code=202)
def import_youtube(request: YouTubeRequest) -> dict[str, Any]:
    try:
        url = validate_youtube_url(request.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "original_name": "YouTube動画",
        "source": "youtube",
        "youtube_url": url,
        "status": "queued_for_youtube_download",
        "progress": {"percent": 0, "message": "YouTube動画の読み込みを待っています"},
    }
    write_job(job)
    executor.submit(run_youtube_download, job_id, url)
    return job


class RenderRequest(BaseModel):
    mode: str
    candidate_indices: list[int]
    include_subtitles: bool = True
    titles: dict[int, str] = Field(default_factory=dict)
    montage_title: str = "見どころまとめ"
    subtitle_size: int = Field(default=72, ge=42, le=100)
    subtitle_position: str = "lower"
    title_size: int = Field(default=52, ge=28, le=200)
    title_margin_top: int = Field(default=90, ge=30, le=600)
    horizontal_positions: dict[int, str] = Field(default_factory=dict)
    ending_text: str = "続きは本編で！"
    ending_size: int = Field(default=96, ge=36, le=200)
    ending_duration: float = Field(default=2.5, ge=0.5, le=10)
    subtitle_overrides: dict[int, str] = Field(default_factory=dict)
    candidate_ranges: dict[int, dict[str, float]] = Field(default_factory=dict)
    excluded_segment_indices: list[int] = Field(default_factory=list)


@app.post("/api/jobs/{job_id}/render", status_code=202)
def start_video_render(job_id: str, request: RenderRequest) -> dict[str, Any]:
    job = read_job(job_id)
    if job["status"] == "rendering_video":
        raise HTTPException(status_code=409, detail="動画はすでに生成中です")
    if request.mode not in {"individual", "montage"}:
        raise HTTPException(status_code=400, detail="生成方式が不正です")
    candidates = job.get("analysis", {}).get("candidates", [])
    indices = list(dict.fromkeys(request.candidate_indices))
    if not indices or any(index < 0 or index >= len(candidates) for index in indices):
        raise HTTPException(status_code=400, detail="生成する候補を選択してください")
    if request.mode == "montage" and len(indices) < 2:
        raise HTTPException(status_code=400, detail="再編集には2件以上の候補を選択してください")
    if request.subtitle_position not in {"upper", "middle", "lower"}:
        raise HTTPException(status_code=400, detail="字幕位置が不正です")
    if any(position not in {"left", "center", "right", "blur"} for position in request.horizontal_positions.values()):
        raise HTTPException(status_code=400, detail="画角位置が不正です")
    updated = update_job(
        job_id,
        status="rendering_video",
        progress={"percent": 0, "message": "動画生成の開始を待っています"},
        error=None,
    )
    executor.submit(
        run_video_render, job_id, request.mode, indices, request.include_subtitles,
        request.titles, request.montage_title[:84], request.subtitle_size,
        request.subtitle_position, request.title_size, request.title_margin_top,
        request.horizontal_positions, request.ending_text[:120], request.ending_size,
        request.ending_duration, request.subtitle_overrides, request.candidate_ranges,
        request.excluded_segment_indices,
    )
    return updated


@app.get("/api/outputs/{filename}")
def get_output(filename: str) -> FileResponse:
    if Path(filename).name != filename or not filename.endswith(".mp4"):
        raise HTTPException(status_code=404, detail="動画が見つかりません")
    path = OUTPUT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="動画が見つかりません")
    return FileResponse(
        path, media_type="video/mp4", filename=filename,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.get("/api/jobs/{job_id}/source")
def get_source_video(job_id: str) -> FileResponse:
    """Stream the source media for in-browser candidate previews."""
    job = read_job(job_id)
    stored_name = str(job.get("stored_name", ""))
    if Path(stored_name).name != stored_name or not stored_name:
        raise HTTPException(status_code=404, detail="元動画が見つかりません")
    path = UPLOAD_DIR / stored_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="元動画が見つかりません")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.delete("/api/jobs/{job_id}/outputs/{filename}")
def delete_output(job_id: str, filename: str) -> dict[str, Any]:
    if Path(filename).name != filename or not filename.endswith(".mp4"):
        raise HTTPException(status_code=404, detail="動画が見つかりません")
    job = read_job(job_id)
    outputs = job.get("outputs", [])
    if not any(output.get("filename") == filename for output in outputs):
        raise HTTPException(status_code=404, detail="このジョブの動画が見つかりません")
    path = OUTPUT_DIR / filename
    if path.exists():
        path.unlink()
    remaining = [output for output in outputs if output.get("filename") != filename]
    update_job(job_id, outputs=remaining)
    return {"deleted": filename, "outputs": remaining}


@app.get("/api/jobs/latest")
def get_latest_job() -> dict[str, Any]:
    paths = list(JOBS_DIR.glob("*.json"))
    if not paths:
        raise HTTPException(status_code=404, detail="履歴はまだありません")
    latest = max(paths, key=lambda path: path.stat().st_mtime)
    return json.loads(latest.read_text(encoding="utf-8"))


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    return read_job(job_id)


@app.post("/api/jobs/{job_id}/transcribe", status_code=202)
def start_transcription(job_id: str) -> dict[str, Any]:
    job = read_job(job_id)
    if job["status"] in {"queued_for_transcription", "transcribing"}:
        raise HTTPException(status_code=409, detail="文字起こしはすでに実行中です")
    if job["status"] not in {"uploaded", "transcribed", "transcription_failed"}:
        raise HTTPException(status_code=409, detail="このジョブは文字起こしできません")
    updated = update_job(
        job_id,
        status="queued_for_transcription",
        progress={"percent": 0, "message": "文字起こしの開始を待っています"},
        error=None,
    )
    executor.submit(run_transcription, job_id)
    return updated


@app.post("/api/jobs/{job_id}/analyze-candidates", status_code=202)
def start_candidate_analysis(job_id: str) -> dict[str, Any]:
    job = read_job(job_id)
    if job["status"] in {"queued_for_analysis", "analyzing_candidates"}:
        raise HTTPException(status_code=409, detail="候補解析はすでに実行中です")
    if job["status"] not in {
        "transcribed", "candidates_analyzed", "candidate_analysis_failed"
    }:
        raise HTTPException(status_code=409, detail="文字起こし完了後に候補解析できます")
    updated = update_job(
        job_id,
        status="queued_for_analysis",
        progress={"percent": 0, "message": "候補解析の開始を待っています"},
        error=None,
    )
    executor.submit(run_candidate_analysis, job_id)
    return updated
