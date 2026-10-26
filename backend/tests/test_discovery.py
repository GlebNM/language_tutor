"""Discovery via public APIs: isolated previews, cached import, cancellation and credentials."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from backend.app.agents.discovery import plan_query
from backend.app.main import create_learner_app
from backend.app.materials import MaterialError
from backend.app.youtube import YouTubeTrack
from backend.app.youtube_search import duration_seconds, search_videos

VIDEO = "LWULB9Aoopc"
URL = f"https://www.youtube.com/watch?v={VIDEO}"
TEXT = "John went to London. I read a book and learn about the world every day. " * 5
SRT = f"1\n00:00:00,000 --> 00:00:30,000\n{TEXT}\n"


def app_client(tmp_path, *, seed=False):
    return TestClient(
        create_learner_app(tmp_path / "learning.sqlite3", include_ai=False, seed_demo=seed),
        base_url="http://localhost",
    )


def finished(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/discovery/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"completed", "cancelled", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail("Discovery did not finish")


def start(client, **kwargs):
    response = client.post("/api/discovery/jobs", json={"query": URL, **kwargs})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def counts(library):
    with library.connect() as db:
        return {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("materials", "lexemes", "word_knowledge", "learning_activity")
        }


def fake_fetch(url):
    return YouTubeTrack(VIDEO, "Sleep science", SRT, "youtube-authored")


def test_key_is_private_persistent_and_reset_removes_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    key = "secret-test-key-123456789"
    with app_client(tmp_path) as client:
        assert not client.get("/api/discovery/settings").json()["configured"]
        assert client.post("/api/discovery/jobs", json={"query": "sleep"}).status_code == 409
        response = client.put("/api/discovery/settings", json={"api_key": key})
        assert response.status_code == 200 and key not in response.text
        path = client.app.state.discovery.key_path
        assert path.stat().st_mode & 0o777 == 0o600
    with app_client(tmp_path) as client:
        assert client.get("/api/discovery/settings").json()["configured"]
        monkeypatch.setattr("backend.app.discovery.fetch_youtube_track", fake_fetch)
        job_id = start(client)
        assert finished(client, job_id)["results"][0]["status"] == "ready"
        assert client.app.state.discovery.cache.get(VIDEO) is not None
        response = client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"})
        assert response.status_code == 200, response.text
        assert not path.exists()
        assert not client.get("/api/discovery/settings").json()["configured"]
        assert client.app.state.discovery.cache.get(VIDEO) is None
        assert client.get(f"/api/discovery/jobs/{job_id}").status_code == 404
        assert client.post(f"/api/discovery/jobs/{job_id}/open/{VIDEO}", json={}).status_code == 404


def test_preview_matches_import_and_rescores_without_registering_words(tmp_path, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return fake_fetch(url)

    monkeypatch.setattr("backend.app.discovery.fetch_youtube_track", fetch)
    with app_client(tmp_path) as client:
        seed = client.post(
            "/api/materials",
            json={
                "youtube_url": "https://youtu.be/TLkA0RELQ1g",
                "title": "Existing material",
                "format": "srt",
                "content": SRT,
            },
        ).json()
        tokens = seed["cues"][0]["tokens"]
        for token in {t["lexeme_id"]: t for t in tokens}.values():
            score = 1 if token["lemma"] == "learn" else None if token["lemma"] == "world" else 3
            client.put(f"/api/lexemes/{token['lexeme_id']}/knowledge", json={"score": score})
        library = client.app.state.materials
        before = counts(library)
        job_id = start(client)
        job = finished(client, job_id)
        preview = job["results"][0]
        assert job["status"] == "completed" and preview["status"] == "ready"
        assert "learn" in preview["review_words"]
        assert counts(library) == before
        # A manual familiarity change affects an existing preview without another fetch.
        book = next(token for token in tokens if token["lemma"] == "book")
        client.put(f"/api/lexemes/{book['lexeme_id']}/knowledge", json={"score": 0})
        revised = client.get(f"/api/discovery/jobs/{job_id}").json()["results"][0]
        assert revised["difficulty"]["known_percent"] < preview["difficulty"]["known_percent"]
        imported = client.post(f"/api/discovery/jobs/{job_id}/open/{VIDEO}", json={})
        assert imported.status_code == 201, imported.text
        assert imported.json()["difficulty"] == revised["difficulty"]
        again = finished(client, start(client))["results"][0]
        assert again["cached"] is True
        assert len(calls) == 1
        assert (
            client.post(f"/api/discovery/jobs/{job_id}/open/{VIDEO}", json={}).json()["id"]
            == imported.json()["id"]
        )


def test_search_partial_failure_keeps_ready_results_and_reuses_search_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "environment-test-key")
    searches = []

    def search(query, key, minimum, maximum):
        searches.append((query, minimum, maximum))
        return [
            {"video_id": VIDEO, "title": "Sleep", "channel": "TED", "duration_seconds": 600},
            {
                "video_id": "jNQXAC9IVRw",
                "title": "Missing",
                "channel": "Other",
                "duration_seconds": 300,
            },
        ]

    def fetch(url):
        if "jNQXAC9IVRw" in url:
            raise MaterialError("У видео нет доступных английских субтитров.")
        return fake_fetch(url)

    monkeypatch.setattr("backend.app.discovery.search_videos", search)
    monkeypatch.setattr("backend.app.discovery.fetch_youtube_track", fetch)
    with app_client(tmp_path) as client:
        job = finished(client, start(client, query="sleep science", minimum_seconds=120))
        assert job["status"] == "completed" and job["checked"] == 2
        assert [r["status"] for r in job["results"]] == ["ready", "unavailable"]
        assert counts(client.app.state.materials)["materials"] == 0
        second = finished(client, start(client, query="sleep science", minimum_seconds=120))
        assert second["results"][0]["cached"] is True
        assert len(searches) == 1


def test_cancel_prevents_late_cache_and_import(tmp_path, monkeypatch):
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()

    def fetch(url):
        entered.set()
        release.wait(3)
        returned.set()
        return fake_fetch(url)

    monkeypatch.setattr("backend.app.discovery.fetch_youtube_track", fetch)
    with app_client(tmp_path) as client:
        job_id = start(client)
        try:
            assert entered.wait(2)
            assert client.post("/api/discovery/jobs", json={"query": URL}).status_code == 409
            assert (
                client.post(f"/api/discovery/jobs/{job_id}/open/{VIDEO}", json={}).status_code
                == 409
            )
            assert client.delete(f"/api/discovery/jobs/{job_id}").json()["status"] == "cancelled"
        finally:
            release.set()
        assert returned.wait(2)
        assert client.app.state.discovery.cache.get(VIDEO) is None
        assert counts(client.app.state.materials)["materials"] == 0


def test_deadline_and_source_restriction_are_actionable(tmp_path, monkeypatch):
    def blocked(url):
        raise MaterialError("YouTube ограничил получение субтитров.")

    monkeypatch.setattr("backend.app.discovery.fetch_youtube_track", blocked)
    with app_client(tmp_path) as client:
        job = finished(client, start(client))
        assert job["results"][0]["status"] == "unavailable"
        assert client.post("/api/discovery/jobs", json={"query": URL}).status_code == 429
        client.app.state.discovery.blocked_until = 0

        def slow(url):
            time.sleep(0.1)
            return fake_fetch(url)

        monkeypatch.setattr("backend.app.discovery.fetch_youtube_track", slow)
        client.app.state.discovery.timeout_seconds = 0.02
        job = finished(client, start(client))
        assert job["status"] == "failed" and "минуту" in job["error"]
        assert client.app.state.discovery.cache.get(VIDEO) is None


def test_query_translation_uses_structured_agent_output():
    def respond(messages, info):
        assert "история игр" in json.dumps(
            [str(message) for message in messages], ensure_ascii=False
        )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "english_query": "history of video games",
                    },
                )
            ]
        )

    assert (
        asyncio.run(plan_query(FunctionModel(respond), "история игр", "low"))
        == "history of video games"
    )


def test_search_batches_metadata_and_filters_language_duration_and_embedding(monkeypatch):
    calls = []

    def get(self, url, *, params):
        calls.append((url, params))
        if url.endswith("/search"):
            data = {
                "items": [
                    {"id": {"videoId": video}}
                    for video in (
                        VIDEO,
                        VIDEO,
                        "jNQXAC9IVRw",
                        "TLkA0RELQ1g",
                        "abcdefghijk",
                    )
                ]
            }
        else:
            data = {
                "items": [
                    {
                        "id": video,
                        "snippet": {
                            "title": video,
                            "channelTitle": "Channel",
                            "defaultAudioLanguage": lang,
                        },
                        "contentDetails": {"duration": length},
                        "status": {"embeddable": embed},
                    }
                    for video, lang, length, embed in (
                        (VIDEO, "en-GB", "PT10M", True),
                        ("jNQXAC9IVRw", "en", "PT19S", True),
                        ("TLkA0RELQ1g", "de", "PT10M", True),
                        ("abcdefghijk", "en", "PT10M", False),
                    )
                ]
            }
        return SimpleNamespace(status_code=200, json=lambda: data)

    monkeypatch.setattr("backend.app.youtube_search.CaptionSession.get", get)
    results = search_videos("space", "secret-test-key", 300, 900)
    assert [r["video_id"] for r in results] == [VIDEO]
    assert len(calls) == 2
    assert calls[0][1]["videoEmbeddable"] == "true"
    assert calls[1][1]["id"] == f"{VIDEO},jNQXAC9IVRw,TLkA0RELQ1g,abcdefghijk"
    assert "secret-test-key" not in json.dumps(results)
    assert duration_seconds("PT1H2M3S") == 3723
    assert duration_seconds("invalid") is None


def test_search_errors_never_expose_key_or_upstream_url(monkeypatch):
    key = "secret-test-key"

    def fail(*args, **kwargs):
        raise requests.ConnectionError(f"https://example.invalid/?key={key}")

    monkeypatch.setattr("backend.app.youtube_search.CaptionSession.get", fail)
    with pytest.raises(MaterialError) as error:
        search_videos("space", key, 0, 900)
    assert key not in str(error.value) and "example.invalid" not in str(error.value)


def test_diagnostic_priors_ignored_and_manual_unknown_match_opened_material(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.discovery.fetch_youtube_track", fake_fetch)
    with app_client(tmp_path) as client:
        seed = client.post(
            "/api/materials",
            json={
                "youtube_url": "https://youtu.be/TLkA0RELQ1g",
                "format": "srt",
                "content": SRT,
            },
        ).json()
        library = client.app.state.materials
        with library.connect() as db:
            for group in (1, 2, 3):
                db.execute("INSERT INTO diagnostic_rules VALUES('en',?,1,1)", (group,))
        for lemma, change in (("book", {"score": None}), ("learn", {"score": 1, "ignored": True})):
            token = next(t for t in seed["cues"][0]["tokens"] if t["lemma"] == lemma)
            client.put(f"/api/lexemes/{token['lexeme_id']}/knowledge", json=change)
        job_id = start(client)
        preview = finished(client, job_id)["results"][0]
        assert preview["difficulty"]["provisional"]
        assert preview["difficulty"]["unassessed"] > 0
        assert "learn" not in preview["review_words"]
        imported = client.post(f"/api/discovery/jobs/{job_id}/open/{VIDEO}", json={}).json()
        assert imported["difficulty"] == preview["difficulty"]


def test_accounts_isolate_search_keys_jobs_cache_and_imports(tmp_path, monkeypatch):
    from backend.app.main import create_app

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr("backend.app.discovery.fetch_youtube_track", fake_fetch)
    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    with (
        TestClient(app, base_url="http://localhost") as alice,
        TestClient(app, base_url="http://localhost") as bob,
    ):
        for client, name in ((alice, "alice"), (bob, "bob")):
            response = client.post(
                "/api/auth/register",
                json={
                    "username": name,
                    "password": "test-password-2026",
                },
            )
            assert response.status_code == 201, response.text
            client.headers["x-session-key"] = response.json()["session_key"]
        assert (
            alice.put("/api/discovery/settings", json={"api_key": "alice-private-key"}).status_code
            == 200
        )
        assert bob.get("/api/discovery/settings").json()["configured"] is False
        alice_job = start(alice)
        finished(alice, alice_job)
        assert bob.get(f"/api/discovery/jobs/{alice_job}").status_code == 404
        assert bob.post(f"/api/discovery/jobs/{alice_job}/open/{VIDEO}", json={}).status_code == 404
        bob_job = start(bob)
        assert finished(bob, bob_job)["results"][0]["cached"] is False
        assert (
            alice.post(f"/api/discovery/jobs/{alice_job}/open/{VIDEO}", json={}).status_code == 201
        )
        assert bob.get("/api/materials").json() == []
        alice.post("/api/data/reset", json={"confirmation": "Удалить мои данные"})
        assert bob.get(f"/api/discovery/jobs/{bob_job}").json()["results"][0]["status"] == "ready"
