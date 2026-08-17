from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_SCOPES = [YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READ_SCOPE]
YOUTUBE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def oauth_config(data_dir: Path | None = None) -> dict[str, Any] | None:
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    if (not client_id or not client_secret) and data_dir:
        client_file = data_dir / "youtube_oauth_client.json"
        if client_file.exists():
            try:
                payload = json.loads(client_file.read_text(encoding="utf-8"))
                details = payload.get("web", payload.get("installed", {}))
                client_id = str(details.get("client_id", "")).strip()
                client_secret = str(details.get("client_secret", "")).strip()
            except (OSError, json.JSONDecodeError):
                pass
    if not client_id or not client_secret:
        return None
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": YOUTUBE_TOKEN_URI,
            "redirect_uris": [oauth_redirect_uri()],
        }
    }


def oauth_redirect_uri() -> str:
    return os.getenv(
        "YOUTUBE_REDIRECT_URI",
        "http://127.0.0.1:8765/api/youtube/oauth/callback",
    ).strip()


def token_directory(data_dir: Path) -> Path:
    return data_dir / "youtube_tokens"


def token_path(data_dir: Path, channel_id: str) -> Path:
    safe_id = "".join(c for c in channel_id if c.isalnum() or c in "_-")
    if not safe_id:
        raise ValueError("YouTubeチャンネルIDが不正です")
    return token_directory(data_dir) / f"{safe_id}.json"


def registry_path(data_dir: Path) -> Path:
    return data_dir / "youtube_channels.json"


def list_channels(data_dir: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(registry_path(data_dir).read_text(encoding="utf-8"))
        return [item for item in payload if item.get("channel_id") and item.get("title")]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def save_credentials(credentials: Any, data_dir: Path, channel_id: str, title: str) -> None:
    directory = token_directory(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = token_path(data_dir, channel_id)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(credentials.to_json(), encoding="utf-8")
    temporary.replace(path)

    channels = [item for item in list_channels(data_dir) if item["channel_id"] != channel_id]
    channels.append({"channel_id": channel_id, "title": title})
    registry = registry_path(data_dir)
    temporary_registry = registry.with_suffix(".json.tmp")
    temporary_registry.write_text(json.dumps(channels, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_registry.replace(registry)


def load_credentials(data_dir: Path, channel_id: str) -> Any | None:
    path = token_path(data_dir, channel_id)
    if not path.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials

        payload = json.loads(path.read_text(encoding="utf-8"))
        credentials = Credentials.from_authorized_user_info(payload, YOUTUBE_SCOPES)
        return credentials if credentials.valid or credentials.refresh_token else None
    except (ImportError, ValueError, json.JSONDecodeError):
        return None


def authorization_url(data_dir: Path | None = None) -> tuple[str, str, str]:
    config = oauth_config(data_dir)
    if not config:
        raise RuntimeError("YouTube OAuth認証情報が未設定です")
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise RuntimeError("Google APIライブラリが未導入です") from exc
    flow = Flow.from_client_config(config, scopes=YOUTUBE_SCOPES)
    flow.redirect_uri = oauth_redirect_uri()
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    if not flow.code_verifier:
        raise RuntimeError("YouTube認証用の一時キーを作成できませんでした")
    return url, state, flow.code_verifier


def exchange_authorization_response(
    response_url: str, state: str, code_verifier: str, data_dir: Path
) -> dict[str, str]:
    config = oauth_config(data_dir)
    if not config:
        raise RuntimeError("YouTube OAuth認証情報が未設定です")
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise RuntimeError("Google APIライブラリが未導入です") from exc
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow = Flow.from_client_config(
        config, scopes=YOUTUBE_SCOPES, state=state, code_verifier=code_verifier
    )
    flow.redirect_uri = oauth_redirect_uri()
    flow.fetch_token(authorization_response=response_url)
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("Google APIライブラリが未導入です") from exc
    youtube = build("youtube", "v3", credentials=flow.credentials, cache_discovery=False)
    response = youtube.channels().list(part="snippet", mine=True).execute()
    items = response.get("items", [])
    if not items:
        raise RuntimeError("選択したYouTubeチャンネルを確認できませんでした")
    channel_id = str(items[0]["id"])
    title = str(items[0].get("snippet", {}).get("title", channel_id))
    save_credentials(flow.credentials, data_dir, channel_id, title)
    return {"channel_id": channel_id, "title": title}


def remove_channel(data_dir: Path, channel_id: str) -> None:
    path = token_path(data_dir, channel_id)
    if path.exists():
        path.unlink()
    channels = [item for item in list_channels(data_dir) if item["channel_id"] != channel_id]
    registry_path(data_dir).write_text(json.dumps(channels, ensure_ascii=False, indent=2), encoding="utf-8")


def upload_video(
    video_path: Path,
    title: str,
    description: str,
    privacy_status: str,
    channel_id: str,
    data_dir: Path,
    progress: Callable[[float, str], None],
) -> dict[str, str]:
    credentials = load_credentials(data_dir, channel_id)
    if not credentials:
        raise RuntimeError("YouTubeと接続されていません")
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise RuntimeError("Google APIライブラリが未導入です") from exc

    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "categoryId": "22",
            },
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
        },
        media_body=media,
    )
    response = None
    while response is None:
        upload_status, response = request.next_chunk()
        if upload_status:
            percent = min(99.0, upload_status.progress() * 100)
            progress(percent, f"YouTubeへアップロード中 {percent:.0f}%")
    video_id = str(response.get("id", ""))
    if not video_id:
        raise RuntimeError("YouTubeから動画IDを取得できませんでした")
    channel_title = next((c["title"] for c in list_channels(data_dir) if c["channel_id"] == channel_id), channel_id)
    save_credentials(credentials, data_dir, channel_id, channel_title)
    return {"video_id": video_id, "url": f"https://youtu.be/{video_id}"}
