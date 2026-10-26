"""URL import and caption editing through public API operations and controlled models."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import IpBlocked, TranscriptsDisabled, VideoUnavailable
from youtube_transcript_api._transcripts import FetchedTranscript, FetchedTranscriptSnippet

from backend.app.agents.subtitles import improve_batch, safe_edit
from backend.app.main import create_learner_app as create_app
from backend.app.materials import MaterialError, MaterialLibrary, parse_track, youtube_id
from backend.app.youtube import CaptionSession, YouTubeTrack, fetch_youtube_track

URL = "https://youtu.be/TLkA0RELQ1g"
SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\ni dont know\n\n2\n00:00:04,000 --> 00:00:06,000\nwe we go\n"
)


def fake_tracks(monkeypatch, *, authored=True):
    def track(code, generated, text):
        fetched = FetchedTranscript(
            snippets=[FetchedTranscriptSnippet(text=text, start=1.25, duration=2.25)],
            video_id="TLkA0RELQ1g",
            language="English",
            language_code=code,
            is_generated=generated,
        )
        return SimpleNamespace(language_code=code, is_generated=generated, fetch=lambda: fetched)

    tracks = [track("de", False, "German"), track("en", True, "automatic")]
    if authored:
        tracks.append(track("en-US", False, "Authored &amp; caption."))
    monkeypatch.setattr(YouTubeTranscriptApi, "list", lambda self, video: tracks)
    monkeypatch.setattr(
        CaptionSession,
        "get",
        lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"title": "Video title"},
        ),
    )


@pytest.mark.parametrize("authored", [True, False])
def test_provider_prioritizes_authored_english_and_keeps_timing(monkeypatch, authored):
    fake_tracks(monkeypatch, authored=authored)
    track = fetch_youtube_track(URL)
    assert track.title == "Video title"
    assert track.source == ("youtube-authored" if authored else "youtube-automatic")
    cues = parse_track(track.content, "srt")
    assert [(c["start_ms"], c["end_ms"]) for c in cues] == [(1250, 3500)]
    assert cues[0]["text"] == ("Authored & caption." if authored else "automatic")


@pytest.mark.parametrize(
    "error, message",
    [
        (IpBlocked("video"), "ограничил"),
        (TranscriptsDisabled("video"), "нет доступных"),
        (VideoUnavailable("video"), "недоступно"),
    ],
)
def test_provider_returns_safe_actionable_errors(monkeypatch, error, message):
    def fail(*args):
        raise error

    monkeypatch.setattr(YouTubeTranscriptApi, "list", fail)
    with pytest.raises(MaterialError, match=message):
        fetch_youtube_track(URL)


@pytest.mark.parametrize(
    "language, url",
    [
        ("de", "https://www.youtube.com/api/timedtext"),
        ("en", "https://www.youtube.com/api/timedtext?kind=forced"),
    ],
)
def test_provider_rejects_non_english_or_forced_tracks(monkeypatch, language, url):
    monkeypatch.setattr(
        YouTubeTranscriptApi,
        "list",
        lambda self, video: [SimpleNamespace(language_code=language, is_generated=False, _url=url)],
    )
    with pytest.raises(MaterialError, match="нет доступных английских"):
        fetch_youtube_track(URL)


@pytest.mark.parametrize(
    "url",
    [
        URL,
        "https://m.youtube.com/watch?v=TLkA0RELQ1g&t=1",
        "https://www.youtube.com/shorts/TLkA0RELQ1g?si=share",
        "https://www.youtube.com/live/TLkA0RELQ1g",
        "https://www.youtube.com/embed/TLkA0RELQ1g",
    ],
)
def test_common_youtube_link_formats(url):
    assert youtube_id(url) == "TLkA0RELQ1g"


def client_app(tmp_path, monkeypatch, *, source="youtube-automatic", model=None):
    monkeypatch.setattr(
        "backend.app.main.fetch_youtube_track",
        lambda url: YouTubeTrack(
            video_id="TLkA0RELQ1g",
            title="From YouTube",
            content=SRT,
            source=source,
        ),
    )
    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    if model:
        app.state.caption_improvement.model_factory = lambda: model
    return app


def wait_job(client, material_id):
    for _ in range(100):
        job = client.get(f"/api/materials/{material_id}/improvement").json()
        if job and job["status"] in {"failed", "completed"}:
            return job
        time.sleep(0.01)
    pytest.fail("Caption job never finished")


def correcting_model(*, review_accepts=True):
    def respond(messages, info):
        prompt = json.loads(messages[-1].parts[0].content)
        ids = list(prompt["targets"])
        is_review = "proposed_edits" in prompt
        output = (
            {"accepted_ids": [ids[0]] if review_accepts else []}
            if is_review
            else {
                "edits": [
                    {"cue_id": ids[0], "text": "I don't know.", "reason": "Apostrophe and case"}
                ]
            }
        )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    return FunctionModel(respond)


def test_url_import_improves_in_background_and_preserves_source_and_timing(tmp_path, monkeypatch):
    app = client_app(tmp_path, monkeypatch, model=correcting_model())
    with TestClient(app, base_url="http://localhost:8765") as client:
        source = client.post("/api/materials/youtube", json={"youtube_url": URL}).json()
        job = wait_job(client, source["id"])
        assert job["status"] == "completed", job
        assert job["completed_batches"] == job["total_batches"] == 1
        assert job["changed_cues"] == 1
        improved = client.get(f"/api/materials/{job['result_id']}").json()
        assert improved["source"] == "youtube-improved"
        assert improved["previous_version_id"] == source["id"]
        assert improved["cues"][0]["text"] == "I don't know."
        assert improved["cues"][1]["text"] == "we we go"
        assert [(c["start_ms"], c["end_ms"]) for c in improved["cues"]] == [
            (c["start_ms"], c["end_ms"]) for c in source["cues"]
        ]
        assert improved["is_complete"] == source["is_complete"]
        assert client.get(f"/api/materials/{source['id']}").json() == source
        assert client.post(f"/api/materials/{source['id']}/improvement", json={}).json() == job
        assert len(client.get("/api/materials").json()) == 2
        for cue in improved["cues"]:
            for token in cue["tokens"]:
                assert cue["text"][token["start_offset"] : token["end_offset"]] == token["surface"]


@pytest.mark.parametrize(
    "source, improve",
    [
        ("youtube-authored", True),
        ("youtube-automatic", False),
    ],
)
def test_authored_and_opt_out_do_not_call_ai(tmp_path, monkeypatch, source, improve):
    app = client_app(tmp_path, monkeypatch, source=source)
    with TestClient(app, base_url="http://localhost:8765") as client:
        response = client.post(
            "/api/materials/youtube",
            json={
                "youtube_url": URL,
                "improve_automatic": improve,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["title"] == "From YouTube"
        material_id = response.json()["id"]
        assert client.get(f"/api/materials/{material_id}/improvement").json() is None
        assert len(client.get("/api/materials").json()) == 1
        if source == "youtube-authored":
            assert (
                client.post(f"/api/materials/{material_id}/improvement", json={}).status_code == 422
            )


def test_import_survives_disconnected_ai_and_retry_is_idempotent(tmp_path, monkeypatch):
    app = client_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost:8765") as client:
        source = client.post("/api/materials/youtube", json={"youtube_url": URL}).json()
        job = wait_job(client, source["id"])
        assert job["status"] == "failed"
        assert "подключите подписку" in job["error"]
        assert client.get(f"/api/materials/{source['id']}").json() == source
        app.state.caption_improvement.model_factory = lambda: correcting_model()
        path = f"/api/materials/{source['id']}/improvement"
        retry = client.post(path, json={}).json()
        assert retry["id"] != job["id"]
        done = wait_job(client, source["id"])
        assert done["status"] == "completed", done
        assert client.post(path, json={}).json() == done


@pytest.mark.parametrize("url", ["https://evil.test/watch?v=TLkA0RELQ1g", "file:///etc/passwd"])
def test_bad_url_never_fetches_or_creates_material(tmp_path, monkeypatch, url):
    app = client_app(tmp_path, monkeypatch)

    def fail(*args):
        pytest.fail("Invalid URLs must not reach the network")

    monkeypatch.setattr("backend.app.main.fetch_youtube_track", fail)
    with TestClient(app, base_url="http://localhost:8765") as client:
        assert client.post("/api/materials/youtube", json={"youtube_url": url}).status_code == 422
        assert client.get("/api/materials").json() == []


async def test_reviewer_rejection_keeps_source_text():
    edits = await improve_batch(
        model=correcting_model(review_accepts=False),
        batch=[{"id": "cue", "text": "i dont know"}],
        context={"before": [], "after": []},
        title="Title",
        reasoning_effort="low",
    )
    assert edits == {}


async def test_foreign_and_duplicate_cue_ids_cannot_be_edited():
    def respond(messages, info):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"edits": [{"cue_id": "neighbor", "text": "Changed.", "reason": "Because"}]},
                )
            ]
        )

    from pydantic_ai.exceptions import UnexpectedModelBehavior

    with pytest.raises(UnexpectedModelBehavior):
        await improve_batch(
            model=FunctionModel(respond),
            batch=[{"id": "cue", "text": "i dont know"}],
            context={"before": [{"cue_id": "neighbor", "text": "Keep."}], "after": []},
            title="Title",
            reasoning_effort="low",
        )


def test_reset_cancels_running_job_and_prevents_late_resurrection(tmp_path, monkeypatch):
    async def slow(messages, info):
        await asyncio.sleep(60)
        return ModelResponse(parts=[])

    app = client_app(tmp_path, monkeypatch, model=FunctionModel(slow))
    with TestClient(app, base_url="http://localhost:8765") as client:
        material = client.post("/api/materials/youtube", json={"youtube_url": URL}).json()
        assert (
            client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"}).status_code
            == 200
        )
        assert client.get("/api/materials").json() == []
        assert client.get(f"/api/materials/{material['id']}/improvement").status_code == 404
        assert not app.state.caption_improvement.tasks


def test_restart_makes_interrupted_job_retryable_and_preserves_material(tmp_path, monkeypatch):
    app = client_app(tmp_path, monkeypatch)
    with TestClient(app, base_url="http://localhost:8765") as client:
        material = client.post(
            "/api/materials/youtube",
            json={
                "youtube_url": URL,
                "improve_automatic": False,
            },
        ).json()
    library = app.state.materials
    with library.connect() as db:
        db.execute(
            "INSERT INTO subtitle_improvements "
            "(source_id,id,status,total_batches,model,prompt_version,created_at) "
            "VALUES(?,?,'running',1,'model','v1','now')",
            (material["id"], "crashed-job"),
        )
    restarted = create_app(library.database_path, seed_demo=False, include_ai=False)
    with TestClient(restarted, base_url="http://localhost:8765") as client:
        job = client.get(f"/api/materials/{material['id']}/improvement").json()
        assert job["status"] == "failed"
        assert "перезапуске" in job["error"]
        assert client.get(f"/api/materials/{material['id']}").json() == material


def test_storage_generation_guards_late_import(tmp_path):
    library = MaterialLibrary(tmp_path / "learning.sqlite3")
    generation = library.get_profile()["storage_generation"]
    library.reset_learning_data()
    with pytest.raises(MaterialError, match="удалены"):
        library.import_track(
            youtube_url=URL, title="", content=SRT, format="srt", expected_generation=generation
        )
    assert library.list_materials() == []


def test_same_text_file_import_does_not_hide_youtube_origin(tmp_path, monkeypatch):
    app = client_app(tmp_path, monkeypatch)
    library = app.state.materials
    original = library.import_track(youtube_url=URL, title="File", content=SRT, format="srt")
    with TestClient(app, base_url="http://localhost:8765") as client:
        imported = client.post(
            "/api/materials/youtube",
            json={
                "youtube_url": URL,
                "improve_automatic": False,
            },
        ).json()
        assert imported["id"] != original["id"]
        assert imported["source"] == "youtube-automatic"
        repeated = client.post(
            "/api/materials/youtube",
            json={
                "youtube_url": URL,
                "improve_automatic": False,
            },
        ).json()
        assert repeated["id"] == imported["id"]
        assert len(client.get("/api/materials").json()) == 2


@pytest.mark.parametrize(
    "before, after",
    [
        ("we we go", "We go."),
        ("it was fifteen", "It was fifty."),
        ("in 2026", "In 2027."),
        ("[Music] we go", "We go."),
        ("we go", "We went to the beach yesterday."),
    ],
)
def test_programmatic_filter_rejects_destructive_edits(before, after):
    assert not safe_edit(before, after)


async def test_improvement_batches_use_original_neighbors_and_keep_all_cues(tmp_path, monkeypatch):
    app = client_app(tmp_path, monkeypatch)
    library = app.state.materials
    content = "\n".join(
        f"{i + 1}\n00:{i // 30:02}:{(i * 2) % 60:02},000 --> "
        f"00:{i // 30:02}:{(i * 2) % 60 + 1:02},000\nwe go home\n"
        for i in range(45)
    )
    material = library.import_track(
        youtube_url=URL, title="Long", content=content, format="srt", source="youtube-automatic"
    )
    contexts = []

    async def respond(messages, info):
        prompt = json.loads(messages[-1].parts[0].content)
        if "proposed_edits" in prompt:
            output = {"accepted_ids": [e["cue_id"] for e in prompt["proposed_edits"]]}
        else:
            contexts.append(prompt["neighbor_context"])
            output = {
                "edits": [
                    {"cue_id": key, "text": "We go home.", "reason": "Case"}
                    for key in prompt["targets"]
                ]
            }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    service = app.state.caption_improvement
    service.model_factory = lambda: FunctionModel(respond)
    first = service.start(material["id"])
    assert service.start(material["id"])["id"] == first["id"]
    await asyncio.gather(*list(service.tasks.values()))
    job = service.get(material["id"])
    assert job["status"] == "completed", job
    assert job["total_batches"] == job["completed_batches"] == 4
    assert contexts[0]["before"] == []
    assert contexts[-1]["after"] == []
    assert contexts[1]["before"][-1]["cue_id"] == material["cues"][11]["id"]
    assert all(
        c["text"] == "we go home" for group in contexts for side in group.values() for c in side
    )
    improved = library.get_material(job["result_id"])
    assert len(improved["cues"]) == len(material["cues"]) == 45
    assert all(c["text"] == "We go home." for c in improved["cues"])


async def test_midway_failure_never_publishes_partial_corrections(tmp_path, monkeypatch):
    app = client_app(tmp_path, monkeypatch)
    library = app.state.materials
    content = "\n".join(
        f"{i + 1}\n00:00:{i * 2:02},000 --> 00:00:{i * 2 + 1:02},000\nwe go home\n"
        for i in range(13)
    )
    material = library.import_track(
        youtube_url=URL,
        title="Partial failure",
        content=content,
        format="srt",
        source="youtube-automatic",
    )

    def respond(messages, info):
        prompt = json.loads(messages[-1].parts[0].content)
        if len(prompt["targets"]) == 1:
            raise RuntimeError("An upstream error containing private details")
        if "proposed_edits" in prompt:
            output = {"accepted_ids": [edit["cue_id"] for edit in prompt["proposed_edits"]]}
        else:
            output = {
                "edits": [
                    {"cue_id": key, "text": "We go home.", "reason": "Case"}
                    for key in prompt["targets"]
                ]
            }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    service = app.state.caption_improvement
    service.model_factory = lambda: FunctionModel(respond)
    service.start(material["id"])
    await asyncio.gather(*list(service.tasks.values()))
    job = service.get(material["id"])
    assert job["status"] == "failed"
    assert job["completed_batches"] == 1
    assert job["result_id"] is None
    assert "private" not in job["error"]
    assert library.get_material(material["id"]) == material
    assert len(library.list_materials()) == 1
