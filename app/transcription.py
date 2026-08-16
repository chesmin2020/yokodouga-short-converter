from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

ProgressCallback = Callable[[float, str], None]


def transcribe_media(path: Path, progress: ProgressCallback) -> dict[str, Any]:
    """Transcribe a media file locally with faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper がインストールされていません。"
            "requirements.txt を再インストールしてください。"
        ) from exc

    model_name = os.getenv("WHISPER_MODEL", "small")
    device = os.getenv("WHISPER_DEVICE", "cpu")
    compute_type = os.getenv(
        "WHISPER_COMPUTE_TYPE", "int8" if device == "cpu" else "float16"
    )
    progress(5, f"モデル ({model_name}) を読み込んでいます")
    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        segments_iter, info = model.transcribe(
            str(path), language="ja", beam_size=5, vad_filter=True
        )
        duration = float(getattr(info, "duration", 0) or 0)
        segments: list[dict[str, Any]] = []
        for segment in segments_iter:
            text = segment.text.strip()
            if text:
                segments.append(
                    {
                        "start": round(float(segment.start), 2),
                        "end": round(float(segment.end), 2),
                        "text": text,
                    }
                )
            percent = 15 + (float(segment.end) / duration * 80 if duration else 0)
            progress(min(95, percent), f"文字起こし中 ({len(segments)} セグメント)")
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise RuntimeError(f"Whisperの実行に失敗しました: {message}") from exc

    return {
        "language": getattr(info, "language", "ja"),
        "language_probability": round(
            float(getattr(info, "language_probability", 0) or 0), 4
        ),
        "duration": round(duration, 2),
        "model": model_name,
        "device": device,
        "compute_type": compute_type,
        "segments": segments,
        "text": "".join(segment["text"] for segment in segments),
    }
