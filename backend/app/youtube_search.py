"""Bounded public YouTube Data API search; credentials never enter result objects."""

from __future__ import annotations

import re
from typing import Any

import requests

from backend.app.materials import MaterialError
from backend.app.youtube import CaptionSession

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def duration_seconds(value: str) -> int | None:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def search_videos(query: str, api_key: str, minimum: int, maximum: int) -> list[dict[str, Any]]:
    def request(session: CaptionSession, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = session.get(
                f"https://www.googleapis.com/youtube/v3/{resource}",
                params={**params, "key": api_key},
            )
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            if response.status_code != 200:
                reasons = {
                    error.get("reason")
                    for error in data.get("error", {}).get("errors", [])
                    if isinstance(error, dict)
                }
                if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
                    raise MaterialError("Дневная квота поиска YouTube исчерпана. Повторите завтра.")
                raise MaterialError(
                    "YouTube не разрешил поиск. Проверьте ключ и включение YouTube Data API v3."
                )
            return data
        except (requests.RequestException, ValueError):
            # Do not expose the upstream exception: its URL may contain the API key.
            raise MaterialError("Поиск YouTube не ответил. Проверьте сеть и повторите.") from None

    with CaptionSession() as session:
        found = request(
            session,
            "search",
            {
                "part": "snippet",
                "type": "video",
                "q": query,
                "relevanceLanguage": "en",
                "videoCaption": "closedCaption",
                "videoEmbeddable": "true",
                "maxResults": 25,
            },
        )
        ids = list(
            dict.fromkeys(
                item.get("id", {}).get("videoId", "")
                for item in found.get("items", [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), dict)
                and VIDEO_ID.fullmatch(item["id"].get("videoId", ""))
            )
        )
        if not ids:
            return []
        details = request(
            session,
            "videos",
            {
                "part": "snippet,contentDetails,status",
                "id": ",".join(ids),
            },
        )
    indexed = {item.get("id"): item for item in details.get("items", []) if isinstance(item, dict)}
    results = []
    for video_id in ids:
        item = indexed.get(video_id, {})
        snippet = item.get("snippet", {})
        seconds = duration_seconds(item.get("contentDetails", {}).get("duration", ""))
        audio_language = snippet.get("defaultAudioLanguage", "").split("-")[0].lower()
        if (
            seconds is None
            or not minimum <= seconds <= maximum
            or item.get("status", {}).get("embeddable") is False
            or audio_language not in {"", "en"}
            or snippet.get("liveBroadcastContent", "none") != "none"
        ):
            continue
        results.append(
            {
                "video_id": video_id,
                "title": str(snippet.get("title", video_id))[:200],
                "channel": str(snippet.get("channelTitle", ""))[:200],
                "duration_seconds": seconds,
            }
        )
    return results[:10]
