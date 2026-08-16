from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

ProgressCallback = Callable[[float, str], None]

POSITIVE_WORDS = (
    "実は", "結論", "一番", "驚", "比較", "違", "デメリット", "注意",
    "失敗", "おすすめ", "理由", "方法", "ポイント", "お得", "正直", "本音",
    "意外", "しかし", "だけど", "なぜ", "どうして", "？", "?",
)
NEGATIVE_WORDS = (
    "こんにちは", "こんばんは", "どうも", "チャンネル登録", "高評価",
    "スポンサー", "提供", "広告", "概要欄", "ご視聴ありがとう",
)


def _candidate_score(text: str, duration: float) -> tuple[int, list[str]]:
    score = 48
    reasons: list[str] = []
    if 30 <= duration <= 60:
        score += 22
        reasons.append("30〜60秒でショートに適した尺")
    elif 20 <= duration <= 75:
        score += 10
    score += min(12, sum(1 for word in POSITIVE_WORDS if word in text) * 3)
    if any(word in text for word in POSITIVE_WORDS):
        reasons.append("比較・結論・意外性につながる言葉がある")
    if len(text) >= 100:
        score += 7
        reasons.append("単体で内容を伝えられる情報量がある")
    if text.rstrip().endswith(("。", "！", "!", "？", "?")):
        score += 5
    if any(word in text for word in NEGATIVE_WORDS):
        score -= 25
        reasons.append("挨拶・広告表現を含むため減点")
    return max(0, min(100, score)), reasons


def _summary(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text)
    sentence = re.split(r"[。！!？?]", cleaned)[0]
    return (sentence[:42] + "…") if len(sentence) > 42 else sentence


def _hook(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text)
    for marker in POSITIVE_WORDS:
        position = cleaned.find(marker)
        if position >= 0:
            return cleaned[position:position + 52].rstrip("。")
    return cleaned[:52].rstrip("。")


def analyze_locally(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build useful candidates without sending transcript text to a cloud API."""
    if not segments:
        return []
    windows: list[dict[str, Any]] = []
    step = max(1, len(segments) // 80)
    for start_index in range(0, len(segments), step):
        start = float(segments[start_index]["start"])
        selected: list[dict[str, Any]] = []
        for segment in segments[start_index:]:
            selected.append(segment)
            duration = float(segment["end"]) - start
            if duration >= 30:
                text = "".join(str(item["text"]).strip() for item in selected)
                score, reasons = _candidate_score(text, duration)
                windows.append({
                    "start": round(start, 2),
                    "end": round(float(segment["end"]), 2),
                    "duration": round(duration, 2),
                    "score": score,
                    "summary": _summary(text),
                    "reason": "、".join(reasons) or "ひとつの話題としてまとまっている",
                    "hook": _hook(text),
                })
                if duration >= 60:
                    break
    ranked: list[dict[str, Any]] = []
    for candidate in sorted(windows, key=lambda item: item["score"], reverse=True):
        overlap = False
        for chosen in ranked:
            intersection = max(0, min(candidate["end"], chosen["end"]) - max(candidate["start"], chosen["start"]))
            shorter = min(candidate["duration"], chosen["duration"])
            if shorter and intersection / shorter > 0.55:
                overlap = True
                break
        if not overlap:
            ranked.append(candidate)
        if len(ranked) == 8:
            break
    return ranked


def _validate_candidates(raw: Any, duration: float) -> list[dict[str, Any]]:
    items = raw.get("candidates", []) if isinstance(raw, dict) else []
    valid: list[dict[str, Any]] = []
    for item in items[:10]:
        try:
            start = max(0.0, float(item["start"]))
            end = min(duration, float(item["end"])) if duration else float(item["end"])
            if end <= start:
                continue
            valid.append({
                "start": round(start, 2),
                "end": round(end, 2),
                "duration": round(end - start, 2),
                "score": max(0, min(100, int(item["score"]))),
                "summary": str(item["summary"])[:120],
                "reason": str(item["reason"])[:240],
                "hook": str(item["hook"])[:160],
            })
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(valid, key=lambda item: item["score"], reverse=True)


def analyze_with_openai(
    transcript: dict[str, Any], progress: ProgressCallback
) -> tuple[list[dict[str, Any]], str]:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openaiライブラリがインストールされていません") from exc

    model = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")
    lines = [
        f"[{float(s['start']):.2f}-{float(s['end']):.2f}] {s['text']}"
        for s in transcript.get("segments", [])
    ]
    progress(20, f"OpenAI ({model}) で動画全体を評価中")
    prompt = """YouTube Shorts/TikTok/Reels向けの候補を5〜10件選び、JSONのみ返してください。
前後の文脈なしで理解でき、冒頭の引きが強く、結論・比較・本音・失敗・意外性・How-toのいずれかがある区間を優先します。
30〜60秒を優先し、挨拶・広告・冗長な前置きは除外し、候補を重複させないでください。
必ず {"candidates":[{"start":0,"end":45,"score":90,"summary":"...","reason":"...","hook":"..."}]} 形式にします。

文字起こし:
""" + "\n".join(lines)
    try:
        client = OpenAI(timeout=180.0)
        response = client.responses.create(
            model=model,
            reasoning={"effort": "low"},
            input=prompt,
            text={"format": {"type": "json_object"}},
        )
        parsed = json.loads(response.output_text)
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise RuntimeError(f"OpenAI APIでの候補解析に失敗しました: {message}") from exc
    candidates = _validate_candidates(parsed, float(transcript.get("duration", 0) or 0))
    if not candidates:
        raise RuntimeError("OpenAI APIから有効な候補が返りませんでした")
    return candidates, model


def analyze_transcript(
    transcript: dict[str, Any], progress: ProgressCallback
) -> dict[str, Any]:
    if not transcript.get("segments"):
        raise RuntimeError("文字起こしセグメントがありません")
    if os.getenv("OPENAI_API_KEY", "").strip():
        candidates, model = analyze_with_openai(transcript, progress)
        engine = "openai"
    else:
        progress(20, "ローカル評価で候補を探しています")
        candidates = analyze_locally(transcript["segments"])
        model = None
        engine = "local"
    if not candidates:
        raise RuntimeError("候補区間を作成できませんでした。音声のあるより長い動画で再試行してください。")
    return {"engine": engine, "model": model, "candidates": candidates}
