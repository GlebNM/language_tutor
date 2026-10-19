#!/usr/bin/env python3
"""Public network diagnostic, not a substitute for the actual Chrome extension trial.

Print metadata and response sizes only. Never print signed caption URLs or transcripts.
No browser profile, cookies, credentials, package installs, or third-party proxy is used.
"""

import argparse
from datetime import datetime, timezone
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


MAX_BYTES = 2_000_000


def get(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
    with urlopen(request, timeout=15) as response:
        return response.status, response.read(MAX_BYTES + 1)


def inspect(video_id):
    result = {
        "video_id": video_id,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "environment": "python-public-http-without-browser-cookies",
        "chrome_trial": "blocked-no-chrome-surface",
    }
    status, page = get("https://www.youtube.com/watch?v=" + video_id)
    result["watch_http_status"] = status
    if len(page) > MAX_BYTES:
        return {**result, "outcome": "watch-page-exceeds-diagnostic-limit"}
    text = page.decode("utf-8", errors="replace")
    marker = "ytInitialPlayerResponse = "
    start = text.find(marker)
    if start < 0:
        return {**result, "outcome": "player-response-unavailable"}
    player, _ = json.JSONDecoder().raw_decode(text[start + len(marker):])
    details = player.get("videoDetails", {})
    result.update({"title": details.get("title"), "duration_seconds": details.get("lengthSeconds"),
                   "playability": player.get("playabilityStatus", {}).get("status")})
    if details.get("videoId") != video_id:
        return {**result, "outcome": "video-identity-mismatch"}
    tracks = player.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    english = []
    for track in tracks:
        url = urlparse(track.get("baseUrl", ""))
        query = parse_qs(url.query)
        if (re.fullmatch(r"en(?:-[A-Za-z0-9]+)*", track.get("languageCode", ""))
                and url.hostname == "www.youtube.com" and url.path == "/api/timedtext"
                and re.fullmatch(r"en(?:-[A-Za-z0-9]+)*", query.get("lang", [""])[0])
                and "tlang" not in query and query.get("v") == [video_id]):
            english.append(track)
    english.sort(key=lambda track: track.get("kind") == "asr")
    result["english_tracks"] = [{"language": track["languageCode"],
                                  "source": "youtube-automatic" if track.get("kind") == "asr" else "youtube-authored"}
                                 for track in english]
    if not english:
        return {**result, "outcome": "no-english-track"}
    selected = english[0]
    result["selected_source"] = result["english_tracks"][0]["source"]
    url = urlparse(selected["baseUrl"])
    query = parse_qs(url.query)
    query["fmt"] = ["json3"]
    status, body = get(urlunparse(url._replace(query=urlencode(query, doseq=True))))
    result.update({"timedtext_http_status": status, "timedtext_bytes": len(body)})
    if not body:
        return {**result, "outcome": "empty-timedtext-response", "is_complete": False}
    if len(body) > MAX_BYTES:
        return {**result, "outcome": "timedtext-exceeds-limit", "is_complete": False}
    try:
        events = json.loads(body).get("events")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {**result, "outcome": "not-json3", "is_complete": False}
    if not isinstance(events, list):
        return {**result, "outcome": "no-events", "is_complete": False}
    spoken = [event for event in events if any(segment.get("utf8", "").strip() for segment in event.get("segs", []))]
    return {**result, "outcome": "json3-response-received-unverified-in-chrome", "event_count": len(spoken),
            "is_complete": False, "note": "HTTP response availability alone does not verify normalization or Chrome playback."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", nargs="+", help="11-character public YouTube video ID")
    args = parser.parse_args()
    for video_id in args.video_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            parser.error("video_id must contain 11 URL-safe characters")
        try:
            result = inspect(video_id)
        except HTTPError as error:
            result = {"video_id": video_id, "outcome": "http-error", "http_status": error.code}
        except (URLError, TimeoutError, ValueError, KeyError):
            result = {"video_id": video_id, "outcome": "network-or-player-response-error"}
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
