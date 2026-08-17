from __future__ import annotations

import subprocess
import tempfile
import os
import json
import urllib.error
import urllib.request
import re
from pathlib import Path
from typing import Any, Callable

from app.candidate_analysis import POSITIVE_WORDS

ProgressCallback = Callable[[float, str], None]

def video_filter(horizontal_position: str = "center") -> str:
    if horizontal_position == "blur":
        return (
            "split=2[background][foreground];"
            "[background]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:2[blurred];"
            "[foreground]scale=1080:1920:force_original_aspect_ratio=decrease[contained];"
            "[blurred][contained]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
    crop_x = {"left": "0", "center": "(iw-ow)/2", "right": "iw-ow"}.get(
        horizontal_position, "(iw-ow)/2"
    )
    return (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920:{crop_x}:0"
    )


def ffmpeg_binary() -> str:
    configured = os.getenv("FFMPEG_BIN", "").strip()
    if configured:
        return configured
    full = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
    return str(full) if full.exists() else "ffmpeg"


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        message = result.stderr.strip().splitlines()
        detail = message[-1] if message else "不明なFFmpegエラー"
        raise RuntimeError(f"動画の書き出しに失敗しました: {detail}")


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _split_caption(text: str, max_chars: int = 20) -> list[str]:
    manual_lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    manual_lines = [line for line in manual_lines if line]
    if len(manual_lines) > 1:
        return ["\n".join(manual_lines)]
    cleaned = re.sub(r"[ \t]+", " ", text).strip()
    if not cleaned:
        return []
    pieces: list[str] = []
    for sentence in filter(None, re.split(r"(?<=[。！？!?])", cleaned)):
        while len(sentence) > max_chars:
            cut = max_chars
            for marker in ("、", ",", "は", "が", "を", "で", "に"):
                position = sentence.rfind(marker, max_chars // 2, max_chars + 1)
                if position >= 0:
                    cut = position + 1
                    break
            pieces.append(sentence[:cut])
            sentence = sentence[cut:]
        if sentence:
            pieces.append(sentence)
    return pieces


def subtitle_events_for_range(
    segments: list[dict[str, Any]], start: float, end: float, offset: float = 0
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for segment in segments:
        segment_start = max(start, float(segment["start"]))
        segment_end = min(end, float(segment["end"]))
        if segment_end <= segment_start:
            continue
        captions = _split_caption(str(segment.get("text", "")))
        if not captions:
            continue
        total_chars = sum(len(caption) for caption in captions)
        cursor = segment_start
        for index, caption in enumerate(captions):
            if index == len(captions) - 1:
                caption_end = segment_end
            else:
                caption_end = cursor + (segment_end - segment_start) * len(caption) / total_chars
            events.append({
                "start": round(offset + cursor - start, 3),
                "end": round(offset + caption_end - start, 3),
                "text": caption,
            })
            cursor = caption_end
    return events


def write_ass(
    path: Path,
    events: list[dict[str, Any]],
    title: str = "",
    duration: float = 0,
    subtitle_size: int = 100,
    subtitle_position: str = "upper",
    subtitle_y: int | None = None,
    title_size: int = 140,
    title_margin_top: int = 90,
    footer_text: str = "",
    footer_size: int = 119,
    footer_margin_bottom: int = 450,
    ending_text: str = "",
    ending_size: int = 96,
    ending_duration: float = 2.5,
) -> None:
    font = os.getenv("SUBTITLE_FONT", "Hiragino Sans")
    alignment = {"upper": 8, "middle": 5, "lower": 2}.get(subtitle_position, 2)
    margin_v = {"upper": 330, "middle": 0, "lower": 270}.get(subtitle_position, 270)
    if subtitle_y is None:
        subtitle_y = {"upper": 500, "middle": 960, "lower": 1500}.get(subtitle_position, 500)
    subtitle_y = max(200, min(1720, int(subtitle_y)))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{subtitle_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,{alignment},70,70,{margin_v},1
Style: Title,{font},{title_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,-1,0,0,0,100,100,0,0,3,3,0,8,70,70,{title_margin_top},1
Style: Footer,{font},{footer_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,-1,0,0,0,100,100,0,0,3,3,0,2,70,70,{footer_margin_bottom},1
Style: Ending,{font},{ending_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H90000000,-1,0,0,0,100,100,0,0,3,4,0,5,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    title_lines = [re.sub(r"[{}\\]", "", line).strip() for line in title.splitlines()]
    title_lines = [line for line in title_lines if line]
    clean_title = r"\N".join(title_lines)[:84]
    if clean_title and duration > 0:
        if r"\N" not in clean_title and len(clean_title) > 21:
            clean_title = clean_title[:21] + r"\N" + clean_title[21:]
        rows.append(
            f"Dialogue: 1,0:00:00.00,{_ass_time(duration)},Title,,0,0,0,,{clean_title}\n"
        )
    footer_lines = [re.sub(r"[{}\\]", "", line).strip() for line in footer_text.splitlines()]
    clean_footer = r"\N".join(line for line in footer_lines if line)[:120]
    if clean_footer and duration > 0:
        rows.append(
            f"Dialogue: 1,0:00:00.00,{_ass_time(duration)},Footer,,0,0,0,,{clean_footer}\n"
        )
    ending_lines = [re.sub(r"[{}\\]", "", line).strip() for line in ending_text.splitlines()]
    clean_ending = r"\N".join(line for line in ending_lines if line)[:120]
    if clean_ending and duration > 0:
        ending_start = max(0.0, duration - ending_duration)
        rows.append(
            f"Dialogue: 2,{_ass_time(ending_start)},{_ass_time(duration)},Ending,,0,0,0,,{clean_ending}\n"
        )
    for event in events:
        text = str(event["text"]).replace("\\", "").replace("{", "").replace("}", "")
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")
        if r"\N" not in text and len(text) > 12:
            text = text[:12] + r"\N" + text[12:]
        rows.append(
            f"Dialogue: 0,{_ass_time(float(event['start']))},{_ass_time(float(event['end']))},Default,,0,0,0,,"
            f"{{\\an5\\pos(540,{subtitle_y})}}{text}\n"
        )
    path.write_text(header + "".join(rows), encoding="utf-8")


def _subtitle_filter(path: Path, horizontal_position: str = "center") -> str:
    escaped = str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"{video_filter(horizontal_position)},subtitles=filename='{escaped}'"


def _render_clip(
    source: Path,
    destination: Path,
    start: float,
    end: float,
    subtitle_events: list[dict[str, Any]] | None = None,
    title: str = "",
    subtitle_size: int = 100,
    subtitle_position: str = "upper",
    subtitle_y: int = 500,
    title_size: int = 140,
    title_margin_top: int = 90,
    horizontal_position: str = "center",
    footer_text: str = "",
    footer_size: int = 119,
    footer_margin_bottom: int = 450,
    ending_text: str = "",
    ending_size: int = 96,
    ending_duration: float = 2.5,
) -> None:
    render_filter = video_filter(horizontal_position)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if subtitle_events or title or footer_text or ending_text:
        temporary = tempfile.TemporaryDirectory(prefix="short_subtitles_")
        ass_path = Path(temporary.name) / "subtitles.ass"
        write_ass(
            ass_path, subtitle_events or [], title, end - start,
            subtitle_size, subtitle_position, subtitle_y, title_size, title_margin_top,
            footer_text, footer_size, footer_margin_bottom,
            ending_text, ending_size, ending_duration,
        )
        render_filter = _subtitle_filter(ass_path, horizontal_position)
    _run_ffmpeg([
        ffmpeg_binary(), "-y", "-ss", f"{start:.3f}", "-i", str(source),
        "-t", f"{end - start:.3f}", "-vf", render_filter,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(destination),
    ])
    if temporary:
        temporary.cleanup()


def choose_montage_clips(
    candidates: list[dict[str, Any]], segments: list[dict[str, Any]]
) -> list[dict[str, float]]:
    """Choose one concise highlight from each selected candidate."""
    clips: list[dict[str, float]] = []
    for candidate_index, candidate in enumerate(candidates[:5]):
        start = float(candidate["start"])
        end = float(candidate["end"])
        matching = [
            segment for segment in segments
            if float(segment["end"]) > start and float(segment["start"]) < end
        ]
        if not matching:
            continue
        best = max(
            matching,
            key=lambda segment: sum(
                1 for word in POSITIVE_WORDS if word in str(segment.get("text", ""))
            ),
        )
        center = (float(best["start"]) + float(best["end"])) / 2
        clip_duration = min(15.0, max(8.0, end - start))
        clip_start = max(start, center - clip_duration / 2)
        clip_end = min(end, clip_start + clip_duration)
        clip_start = max(start, clip_end - clip_duration)
        proposed = {
            "start": round(clip_start, 2), "end": round(clip_end, 2),
            "candidate_index": candidate_index,
        }
        if not any(
            max(0, min(proposed["end"], old["end"]) - max(proposed["start"], old["start"])) > 3
            for old in clips
        ):
            clips.append(proposed)
    return clips


def _kept_ranges(
    start: float,
    end: float,
    segments: list[dict[str, Any]],
    excluded_segment_indices: list[int],
) -> list[dict[str, float]]:
    """Subtract explicitly deleted transcript intervals from a source range."""
    excluded = []
    for index in set(excluded_segment_indices):
        if 0 <= index < len(segments):
            segment = segments[index]
            cut_start = max(start, float(segment["start"]))
            cut_end = min(end, float(segment["end"]))
            if cut_end > cut_start:
                excluded.append((cut_start, cut_end))
    excluded.sort()
    merged: list[list[float]] = []
    for cut_start, cut_end in excluded:
        if merged and cut_start <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], cut_end)
        else:
            merged.append([cut_start, cut_end])
    ranges: list[dict[str, float]] = []
    cursor = start
    for cut_start, cut_end in merged:
        if cut_start > cursor + 0.05:
            ranges.append({"start": cursor, "end": cut_start})
        cursor = max(cursor, cut_end)
    if end > cursor + 0.05:
        ranges.append({"start": cursor, "end": end})
    return ranges


def _concat_rendered(parts: list[Path], destination: Path, temp_dir: Path) -> None:
    if len(parts) == 1:
        parts[0].replace(destination)
        return
    concat_file = temp_dir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in parts), encoding="utf-8"
    )
    _run_ffmpeg([
        ffmpeg_binary(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c", "copy", "-movflags", "+faststart", str(destination),
    ])


def _post_metadata(
    title: str,
    candidates: list[dict[str, Any]],
    montage: bool = False,
    source_title: str = "",
    source_description: str = "",
    source_url: str = "",
    source_channel: str = "",
) -> dict[str, str]:
    summaries = [str(item.get("summary", "")).strip() for item in candidates]
    summaries = [summary for summary in summaries if summary]
    hooks = [str(item.get("hook", "")).strip() for item in candidates]
    hooks = [hook for hook in hooks if hook]
    post_title = (title.strip() or ("見どころまとめ" if montage else (summaries[0] if summaries else "ショート動画")))
    if not post_title.endswith(("！", "？", "!", "?")):
        post_title += "｜Shorts"
    topic = title.strip() or (summaries[0] if summaries else "今回の見どころ")
    excerpt = str(candidates[0].get("transcript_excerpt", "")).strip() if candidates else ""
    coherent_excerpt = re.sub(r"\s+", "", excerpt)
    coherent_excerpt = coherent_excerpt[:100].rstrip("、。 ")

    lines = [f"今回のショートでは「{topic[:100]}」を紹介します。"]
    if montage and summaries:
        lines.extend(["", "見どころ"])
        lines.extend(f"・{summary}" for summary in summaries[:5])
    elif coherent_excerpt:
        lines.extend(["", coherent_excerpt + "。"])
    reference_lines = []
    for line in source_description.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith(("http://", "https://", "#")):
            continue
        if cleaned not in reference_lines:
            reference_lines.append(cleaned[:160])
        if len(reference_lines) == 3:
            break
    if source_title or reference_lines:
        lines.extend(["", "本編について"])
        if source_title:
            lines.append(f"『{source_title[:120]}』から見どころを紹介しています。")
        lines.extend(reference_lines)
    if source_channel:
        lines.extend(["", f"チャンネル：{source_channel}"])
    if source_url:
        lines.extend(["", f"本編はこちら：{source_url}"])
    lines.extend([
        "", "感想や気になったポイントを、ぜひコメントで教えてください。",
        "チャンネル登録・高評価もよろしくお願いします！",
        "", "#Shorts #ショート動画",
    ])
    youtube_description = "\n".join(lines).strip()

    instagram_lines = [
        f"【{title.strip() or topic}】",
        "", f"{topic}",
    ]
    if coherent_excerpt:
        instagram_lines.extend(["", coherent_excerpt + "。"])
    instagram_lines.extend([
        "", "あとで見返せるように保存がおすすめです📌",
        "感想はコメントで教えてください！",
        "", "#リール #動画 #おすすめ #学び #InstagramReels",
    ])

    tiktok_hook = topic
    tiktok_lines = [
        f"{tiktok_hook} 👀",
        "", f"今回は「{topic}」を短くまとめました。",
        "最後まで見て、あなたの感想をコメントで教えてください！",
        "", "#TikTok #おすすめ #おすすめにのりたい #動画 #ショート動画",
    ]
    return {
        "post_title": post_title[:100],
        "description": youtube_description,
        "youtube_description": youtube_description,
        "instagram_caption": "\n".join(instagram_lines).strip(),
        "tiktok_caption": "\n".join(tiktok_lines).strip(),
    }


def generate_post_metadata(
    title: str,
    candidates: list[dict[str, Any]],
    montage: bool = False,
    source_title: str = "",
    source_description: str = "",
    source_url: str = "",
    source_channel: str = "",
) -> dict[str, str]:
    """Understand the selected transcript as a whole and write platform-specific copy."""
    fallback = _post_metadata(
        title, candidates, montage, source_title, source_description, source_url, source_channel
    )
    selected_sections = []
    for index, candidate in enumerate(candidates, 1):
        selected_sections.append(
            f"[選択区間{index}の編集確定後テキスト]\n"
            f"{candidate.get('transcript_excerpt', '')}"
        )
    prompt = f"""以下はショート動画に実際に含まれる字幕全文と、元YouTube動画の情報です。
「編集確定後テキスト」が内容判断の最優先かつ唯一の正解です。
編集で削除された文章や、編集前にしか存在しない情報は投稿文へ含めないでください。
字幕を箇条書きで切り貼りせず、前後をつなげて話題・主張・結論を理解してください。
元動画の概要欄は固有名詞や背景を理解する補助情報としてのみ使い、編集確定後テキストにない話題を追加しないでください。
意味が曖昧な字幕は自然に補正して要約し、誇張・断定・架空の情報は避けてください。
文字起こし特有の言い直し・相づち・重複を除き、投稿文だけを読んでも内容が一貫して分かる文章にしてください。
字幕に結論がない場合は結論を創作せず、「動画では〜について話しています」と表現してください。
字幕の文章をそのまま連結・転載してはいけません。内容を理解したうえで、別の簡潔な日本語に言い換えてください。
YouTube概要欄の内容要約は2〜3文、合計120〜220文字程度に収め、主題・重要点・結論の順で構成してください。
InstagramとTikTokも字幕の引用ではなく、各媒体向けに短く要約してください。
字幕に明記されていない操作手順、設定画面、効果、注意事項、商品情報を補ってはいけません。
「一般的には正しそうな知識」も追加せず、この字幕で実際に話されている範囲だけを要約してください。

次のJSONだけを返してください。
{{
  "post_title": "YouTube Shorts向け100文字以内のタイトル",
  "youtube_description": "自然な導入、内容の要約、本編への導線、コメント促進、関連ハッシュタグを含む概要欄",
  "instagram_caption": "冒頭フック、読みやすい要約、保存・コメント促進、厳選したハッシュタグを含むReels投稿文",
  "tiktok_caption": "短いフック、内容の核心、コメント促進、厳選したハッシュタグを含むTikTok投稿文"
}}

固定テロップ: {title}
元動画タイトル: {source_title[:300]}
元チャンネル: {source_channel[:200]}
元動画URL: {source_url[:500]}
元YouTube概要欄:
{source_description[:2500]}

ショートに含まれる内容:
{chr(10).join(selected_sections)}
"""

    def validated(parsed: dict[str, Any], engine: str, model: str) -> dict[str, str] | None:
        post_title = str(parsed.get("post_title", "")).strip()[:100]
        youtube_description = str(parsed.get("youtube_description", "")).strip()[:5000]
        instagram_caption = str(parsed.get("instagram_caption", "")).strip()[:2200]
        tiktok_caption = str(parsed.get("tiktok_caption", "")).strip()[:2200]
        if not all((post_title, youtube_description, instagram_caption, tiktok_caption)):
            return None
        return {
            "post_title": post_title,
            "description": youtube_description,
            "youtube_description": youtube_description,
            "instagram_caption": instagram_caption,
            "tiktok_caption": tiktok_caption,
            "metadata_engine": engine,
            "metadata_model": model,
        }

    ollama_model = os.getenv("OLLAMA_MODEL", "qwen3:4b").strip()
    ollama_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        request = urllib.request.Request(
            f"{ollama_url}/api/generate",
            data=json.dumps({
                "model": ollama_model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "format": "json",
                "options": {"temperature": 0.1, "num_predict": 900},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=240) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = validated(json.loads(payload["response"]), "ollama", ollama_model)
        if result:
            return result
    except (OSError, KeyError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        pass

    if not os.getenv("OPENAI_API_KEY", "").strip():
        return fallback
    try:
        from openai import OpenAI

        model = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")
        response = OpenAI(timeout=180.0).responses.create(
            model=model,
            reasoning={"effort": "low"},
            input=prompt,
            text={"format": {"type": "json_object"}},
        )
        parsed = json.loads(response.output_text)
        return validated(parsed, "openai", model) or fallback
    except Exception:
        return fallback


def render_individual(
    source: Path,
    output_dir: Path,
    job_id: str,
    candidates: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    include_subtitles: bool,
    titles: list[str],
    subtitle_size: int,
    subtitle_position: str,
    subtitle_y: int,
    title_size: int,
    title_margin_top: int,
    horizontal_positions: list[str],
    footer_text: str,
    footer_size: int,
    footer_margin_bottom: int,
    ending_text: str,
    ending_size: int,
    ending_duration: float,
    excluded_segment_indices: list[int],
    progress: ProgressCallback,
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates, start=1):
        destination = output_dir / f"{job_id}_short_{index:02d}.mp4"
        progress((index - 1) / len(candidates) * 90, f"候補 {index}/{len(candidates)} を生成中")
        start, end = float(candidate["start"]), float(candidate["end"])
        ranges = _kept_ranges(start, end, segments, excluded_segment_indices)
        if not ranges:
            raise RuntimeError("削除後に動画として残る区間がありません")
        title = titles[index - 1] if index - 1 < len(titles) else ""
        horizontal_position = (
            horizontal_positions[index - 1] if index - 1 < len(horizontal_positions) else "center"
        )
        with tempfile.TemporaryDirectory(prefix=f"{job_id}_individual_", dir=output_dir) as temp:
            temp_dir = Path(temp)
            parts = []
            for part_index, source_range in enumerate(ranges):
                part = temp_dir / f"part_{part_index:03d}.mp4"
                subtitle_events = (
                    subtitle_events_for_range(segments, source_range["start"], source_range["end"])
                    if include_subtitles else None
                )
                _render_clip(
                    source, part, source_range["start"], source_range["end"],
                    subtitle_events, title, subtitle_size, subtitle_position, subtitle_y,
                    title_size, title_margin_top, horizontal_position,
                    footer_text, footer_size, footer_margin_bottom,
                    ending_text if part_index == len(ranges) - 1 else "",
                    ending_size, ending_duration,
                )
                parts.append(part)
            _concat_rendered(parts, destination, temp_dir)
        output_duration = round(sum(item["end"] - item["start"] for item in ranges), 3)
        metadata = _post_metadata(title, [candidate])
        outputs.append({
            "filename": destination.name,
            "type": "individual",
            "start": candidate["start"],
            "end": candidate["end"],
            "duration": output_duration,
            "source_ranges": ranges,
            "subtitles": include_subtitles,
            "title": title,
            **metadata,
        })
    return outputs


def render_montage(
    source: Path,
    output_dir: Path,
    job_id: str,
    candidates: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    include_subtitles: bool,
    title: str,
    subtitle_size: int,
    subtitle_position: str,
    subtitle_y: int,
    title_size: int,
    title_margin_top: int,
    horizontal_positions: list[str],
    footer_text: str,
    footer_size: int,
    footer_margin_bottom: int,
    ending_text: str,
    ending_size: int,
    ending_duration: float,
    excluded_segment_indices: list[int],
    progress: ProgressCallback,
) -> list[dict[str, Any]]:
    clips = choose_montage_clips(candidates, segments)
    if len(clips) < 2:
        raise RuntimeError("再編集には重複していない2件以上の候補が必要です")
    destination = output_dir / f"{job_id}_montage.mp4"
    with tempfile.TemporaryDirectory(prefix=f"{job_id}_", dir=output_dir) as temp:
        temp_dir = Path(temp)
        rendered: list[Path] = []
        kept_clips = []
        for clip in clips:
            for kept in _kept_ranges(clip["start"], clip["end"], segments, excluded_segment_indices):
                kept["candidate_index"] = clip["candidate_index"]
                kept_clips.append(kept)
        if not kept_clips:
            raise RuntimeError("削除後に動画として残る区間がありません")
        for index, clip in enumerate(kept_clips, start=1):
            progress((index - 1) / len(kept_clips) * 80, f"見どころ {index}/{len(kept_clips)} を生成中")
            clip_path = temp_dir / f"clip_{index:03d}.mp4"
            subtitle_events = subtitle_events_for_range(segments, clip["start"], clip["end"]) if include_subtitles else None
            candidate_index = int(clip["candidate_index"])
            horizontal_position = (
                horizontal_positions[candidate_index]
                if candidate_index < len(horizontal_positions) else "center"
            )
            _render_clip(source, clip_path, clip["start"], clip["end"], subtitle_events,
                         title, subtitle_size, subtitle_position, subtitle_y, title_size,
                         title_margin_top, horizontal_position,
                         footer_text, footer_size, footer_margin_bottom,
                         ending_text if index == len(kept_clips) else "",
                         ending_size, ending_duration)
            rendered.append(clip_path)
        progress(85, "見どころを1本の動画につないでいます")
        _concat_rendered(rendered, destination, temp_dir)
    duration = round(sum(clip["end"] - clip["start"] for clip in kept_clips), 2)
    metadata = _post_metadata(title, candidates, montage=True)
    return [{
        "filename": destination.name,
        "type": "montage",
        "duration": duration,
        "clips": kept_clips,
        "subtitles": include_subtitles,
        "title": title,
        **metadata,
    }]
