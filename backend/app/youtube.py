"""Fetch public English captions without downloading video or translating a track."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    IpBlocked,
    NoTranscriptFound,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
)

from backend.app.materials import MAX_CUES, MAX_FILE_BYTES, MaterialError, youtube_id


@dataclass(frozen=True)
class YouTubeTrack:
    video_id: str
    title: str
    content: str
    source: str


class CaptionSession(requests.Session):
    def __init__(self) -> None:
        super().__init__()
        # Share the deployment's explicit CA bundle with the existing httpx transport.
        if ca_file := os.getenv("SSL_CERT_FILE"):
            self.verify = ca_file

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        # The upstream library has no default timeout or response size limit.
        kwargs["timeout"] = (4, 8)
        kwargs["stream"] = True
        response = super().request(*args, **kwargs)
        try:
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 5_000_000:
                    raise MaterialError("Ответ YouTube слишком большой. Попробуйте импорт SRT/VTT.")
                chunks.append(chunk)
            response._content = b"".join(chunks)
            response._content_consumed = True
            return response
        finally:
            response.close()


def srt_timestamp(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def fetch_youtube_track(url: str) -> YouTubeTrack:
    video_id = youtube_id(url)
    try:
        with CaptionSession() as session:
            tracks = list(YouTubeTranscriptApi(http_client=session).list(video_id))
            english = [
                t
                for t in tracks
                if t.language_code.split("-")[0].lower() == "en"
                # 1.2.4 exposes kind only in the source URL. Forced captions are incomplete.
                and parse_qs(urlparse(getattr(t, "_url", "")).query).get("kind") != ["forced"]
            ]
            if not english:
                raise MaterialError(
                    "У видео нет доступных английских субтитров. Добавьте файл SRT/VTT."
                )
            english.sort(key=lambda t: (t.is_generated, t.language_code != "en", t.language_code))
            track = english[0]
            fetched = track.fetch()
            if not fetched or len(fetched) > MAX_CUES:
                raise MaterialError("Дорожка пустая или содержит больше 10 000 реплик.")
            blocks: list[str] = []
            size = 0
            for index, snippet in enumerate(fetched, 1):
                if (
                    not math.isfinite(snippet.start)
                    or not math.isfinite(snippet.duration)
                    or snippet.start < 0
                    or snippet.duration <= 0
                ):
                    raise MaterialError("YouTube вернул неверные временные метки субтитров.")
                start = round(snippet.start * 1000)
                end = max(start + 1, round((snippet.start + snippet.duration) * 1000))
                text = " ".join(snippet.text.split())
                block = f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n"
                size += len(block.encode("utf-8")) + 1
                if size > MAX_FILE_BYTES:
                    raise MaterialError("Субтитры должны занимать не больше 2 МБ.")
                blocks.append(block)
            title = video_id
            try:
                metadata = session.get(
                    "https://www.youtube.com/oembed",
                    params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
                )
                metadata.raise_for_status()
                data = metadata.json()
                value = data.get("title") if isinstance(data, dict) else None
                if isinstance(value, str) and value.strip():
                    title = value.strip()[:200]
            except (requests.RequestException, ValueError):
                pass  # Captions remain useful when optional video metadata is unavailable.
            return YouTubeTrack(
                video_id=video_id,
                title=title,
                content="\n".join(blocks),
                source="youtube-automatic" if track.is_generated else "youtube-authored",
            )
    except (TranscriptsDisabled, NoTranscriptFound):
        raise MaterialError(
            "У видео нет доступных английских субтитров. Добавьте файл SRT/VTT."
        ) from None
    except (IpBlocked, RequestBlocked, PoTokenRequired):
        raise MaterialError(
            "YouTube ограничил получение субтитров. Попробуйте позже или добавьте SRT/VTT."
        ) from None
    except (AgeRestricted, VideoUnavailable, VideoUnplayable):
        raise MaterialError("Видео недоступно или требует входа в YouTube.") from None
    except (CouldNotRetrieveTranscript, requests.RequestException, ValueError) as error:
        if isinstance(error, MaterialError):
            raise
        raise MaterialError(
            "Не удалось получить субтитры YouTube. Повторите запрос или добавьте SRT/VTT."
        ) from None
