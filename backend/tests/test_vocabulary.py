"""Automatic selection through public APIs, persisted data and the real MCP server."""

import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backend.app.main import create_learner_app as create_app
from backend.app.tenant_context import mcp_headers
from backend.tests.material_fixtures import restore_timed_material_schema
from backend.tests.test_diagnostics import diagnostic_mcp as diagnostic_mcp
from backend.tests.test_learning import forbid_live_models as forbid_live_models
from backend.tests.test_learning import learning_mcp as learning_mcp

TEXT = "Hmmm, hello John. Ouch, wow! Um, wooow, book, school."
ALLOWED = {"hello", "ouch", "wow", "book", "school"}


def client_for(path):
    return TestClient(
        create_app(path, seed_demo=False, include_ai=False, frontend_dist=path.parent / "missing"),
        base_url="http://localhost",
    )


def import_text(client, text=TEXT, video="TLkA0RELQ1g"):
    response = client.post(
        "/api/materials",
        json={
            "youtube_url": f"https://youtu.be/{video}",
            "title": "Vocabulary selection",
            "format": "srt",
            "content": f"1\n00:00:00,000 --> 00:00:05,000\n{text}\n",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def token_for(material, surface):
    return next(
        token for cue in material["cues"] for token in cue["tokens"] if token["surface"] == surface
    )


def pick(client, material, token, viewing=None):
    response = client.post(
        "/api/activity/word-selections",
        json={
            "material_id": material["id"],
            "cue_id": material["cues"][0]["id"],
            "token_id": token["id"],
            "viewing_id": viewing,
        },
    )
    assert response.status_code == 200, response.text


def test_categories_keep_source_tokens_and_contextual_names(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        text = (
            "John met Mary in London on Monday. Apple released a phone in May. "
            "I ate an apple. May I leave? Will can help. "
            "Hello! Ouch! Wow! Book school. Hmm hmmmmmmmm ummm uhhh wooow wooooow."
        )
        material = import_text(client, text)
        cue = material["cues"][0]
        assert cue["original_text"] == cue["text"] == text
        for token in cue["tokens"]:
            assert text[token["start_offset"] : token["end_offset"]] == token["surface"]
        for name in ("John", "Mary", "London", "Monday", "Apple"):
            assert token_for(material, name)["vocabulary"]["reason"] == "proper_name"
        months = [t for t in cue["tokens"] if t["surface"] == "May"]
        assert [t["vocabulary"]["eligible"] for t in months] == [False, True]
        for word in ("apple", "Will", "Hello", "Ouch", "Wow", "Book", "school"):
            assert token_for(material, word)["vocabulary"]["eligible"] is True
        for word in ("Hmm", "hmmmmmmmm", "ummm", "uhhh", "wooow", "wooooow"):
            token = token_for(material, word)
            assert token["vocabulary"]["eligible"] is False
            pick(client, material, token)
        assert client.get("/api/dictionary").json() == []


@pytest.mark.parametrize(
    "score,ignored", [(0, False), (1, False), (2, False), (3, False), (None, False), (None, True)]
)
def test_any_manual_status_adds_word_and_survives_restart(tmp_path, score, ignored):
    path = tmp_path / "learning.sqlite3"
    with client_for(path) as client:
        material = import_text(client)
        token = token_for(material, "Hmmm")
        endpoint = f"/api/lexemes/{token['lexeme_id']}/knowledge"
        response = client.put(endpoint, json={"score": score, "ignored": ignored})
        assert response.status_code == 200
        restored = client.get(f"/api/materials/{material['id']}").json()
        selected = token_for(restored, "Hmmm")
        assert selected["id"] == token["id"]
        assert selected["vocabulary"] == {
            "eligible": True,
            "automatically_eligible": False,
            "reason": "filler",
        }
        assert restored["difficulty"]["total"] == (5 if ignored else 6)
        assert [word["lexeme_id"] for word in client.get("/api/dictionary").json()] == [
            token["lexeme_id"]
        ]
        other = import_text(client, video="jNQXAC9IVRw")
        assert token_for(other, "Hmmm")["vocabulary"]["eligible"] is True
        # A manual reset keeps the entry even when its score becomes null.
        client.put(endpoint, json={"score": None, "ignored": False})
    with client_for(path) as client:
        restored = client.get(f"/api/materials/{material['id']}").json()
        assert token_for(restored, "Hmmm")["vocabulary"]["eligible"] is True
        assert len(client.get("/api/dictionary").json()) == 1


def test_summary_and_independent_coverage_use_only_allowed_occurrences(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        material = import_text(client)
        assert material["difficulty"]["total"] == 5
        for surface, score in (("hello", 2), ("Ouch", 0), ("wow", 3), ("school", 2)):
            token = token_for(material, surface)
            client.put(f"/api/lexemes/{token['lexeme_id']}/knowledge", json={"score": score})
        counted = client.get(f"/api/materials/{material['id']}").json()["difficulty"]
        assert [counted[key] for key in ("total", "known", "unfamiliar", "unassessed")] == [
            5,
            3,
            1,
            1,
        ]
        assert [
            counted[key] for key in ("known_percent", "unfamiliar_percent", "unassessed_percent")
        ] == [60, 20, 20]
        john = token_for(material, "John")
        client.put(f"/api/lexemes/{john['lexeme_id']}/knowledge", json={"score": 3})
        counted = client.get(f"/api/materials/{material['id']}").json()["difficulty"]
        assert counted["total"] == 6
        assert counted["known_percent"] == pytest.approx(100 * 4 / 6)

        client.put("/api/settings/learning", json={"enabled": True})
        viewing = client.post(f"/api/materials/{material['id']}/viewing", json={}).json()[
            "viewing_id"
        ]
        for surface in ("Hmmm", "Um", "wooow"):
            pick(client, material, token_for(material, surface), viewing)
        assert client.get(f"/api/viewings/{viewing}").json()["entries"] == []
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [material["cues"][0]["id"]]})
        entries = client.get(f"/api/viewings/{viewing}").json()["entries"]
        assert [(e["lemma"], e["score"]) for e in entries] == [("book", 2)]
        assert client.post(f"/api/viewings/{viewing}/confirm", json={}).json()["saved"] == 1
        assert {word["lemma"] for word in client.get("/api/dictionary").json()} == ALLOWED | {
            "john"
        }


def test_excluded_click_does_not_change_allowed_use_of_same_lexeme(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        material = import_text(client, "Apple released a phone. I ate an apple.")
        name, fruit = token_for(material, "Apple"), token_for(material, "apple")
        # Explicitly unify the lexical identity to exercise occurrence-level selection.
        response = client.put(
            f"/api/materials/{material['id']}/occurrences/{fruit['id']}/lexeme",
            json={
                "cue_id": material["cues"][0]["id"],
                "lemma": name["lemma"],
                "part_of_speech": "PROPN",
            },
        )
        assert response.status_code == 200
        assert response.json()["token"]["lexeme_id"] == name["lexeme_id"]
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = client.post(f"/api/materials/{material['id']}/viewing", json={}).json()[
            "viewing_id"
        ]
        pick(client, material, name, viewing)
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [material["cues"][0]["id"]]})
        apple = next(
            e
            for e in client.get(f"/api/viewings/{viewing}").json()["entries"]
            if e["lexeme_id"] == name["lexeme_id"]
        )
        assert apple["score"] == 2
        assert apple["token_id"] == fruit["id"]


def test_diagnostic_priors_cannot_restore_excluded_words(tmp_path, diagnostic_mcp):
    from backend.tests.test_diagnostics import adaptive_connection, answer_all, diagnostic_client

    path = tmp_path / "api.sqlite3"
    with diagnostic_client(
        path,
        service=adaptive_connection(tmp_path),
        mcp_url=diagnostic_mcp,
    ) as client:
        material = import_text(client)
        session = client.post("/api/diagnostics", json={}).json()
        answer_all(client, session)
        result = client.post(f"/api/diagnostics/{session['id']}/complete", json={})
        assert result.status_code == 200, result.text
        restored = client.get(f"/api/materials/{material['id']}").json()
        for surface in ("John", "Um"):
            token = token_for(restored, surface)
            assert token["knowledge"]["source"] == "prior"
            assert token["vocabulary"]["eligible"] is False
        assert restored["difficulty"]["total"] == 5
        assert {word["lemma"] for word in client.get("/api/dictionary").json()}.isdisjoint(
            {"john", "um", "hmmm", "wooow"}
        )
        john = token_for(restored, "John")
        client.put(f"/api/lexemes/{john['lexeme_id']}/knowledge", json={"score": 0})
        assert (
            client.get("/api/dictionary", params={"search": "john"}).json()[0]["lexeme_id"]
            == john["lexeme_id"]
        )


def test_old_material_and_pending_summary_backfill_preserve_confirmed_entries(tmp_path):
    from backend.app.materials import MIGRATIONS

    path = tmp_path / "learning.sqlite3"
    with client_for(path) as client:
        material = import_text(client)
        saved = token_for(material, "Hmmm")
        client.put(f"/api/lexemes/{saved['lexeme_id']}/knowledge", json={"score": 1})
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = client.post(f"/api/materials/{material['id']}/viewing", json={}).json()[
            "viewing_id"
        ]
        pick(client, material, token_for(material, "wooow"), viewing)
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [material["cues"][0]["id"]]})
        before = {route: client.get(route).json() for route in ("/api/profile", "/api/activity")}
    # A pre-selection database has no cache tables; the pending viewing remains.
    with sqlite3.connect(path) as db:
        for table in (
            "expression_events",
            "expression_jobs",
            "expression_occurrences",
            "expression_knowledge",
            "expressions",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DROP TABLE occurrence_vocabulary")
        db.execute("DROP TABLE message_vocabulary")
        db.execute("DROP TABLE subtitle_improvements")
        db.execute("DROP TABLE playback_intervals")
        db.execute("DROP TABLE progress_vocabulary")
        db.execute("DELETE FROM storage_metadata WHERE key='playback_tracking_since'")
        version = next(
            i
            for i, script in enumerate(MIGRATIONS)
            if "CREATE TABLE occurrence_vocabulary" in script
        )
        restore_timed_material_schema(db)
        db.execute(f"PRAGMA user_version={version}")
    with client_for(path) as client:
        entries = client.get(f"/api/viewings/{viewing}").json()["entries"]
        assert {e["lemma"] for e in entries} == ALLOWED
        assert client.post(f"/api/viewings/{viewing}/confirm", json={}).json()["saved"] == 5
        restored = client.get(f"/api/materials/{material['id']}").json()
        assert [t["id"] for t in restored["cues"][0]["tokens"]] == [
            t["id"] for t in material["cues"][0]["tokens"]
        ]
        assert restored["cues"][0]["text"] == TEXT
        assert token_for(restored, "Hmmm")["knowledge"]["score"] == 1
        assert all(word["lemma"] != "wooow" for word in client.get("/api/dictionary").json())
        assert (
            client.get("/api/profile").json()["storage_generation"]
            == before["/api/profile"]["storage_generation"]
        )
        assert len(client.get("/api/activity").json()["events"]) > len(
            before["/api/activity"]["events"]
        )
        assert import_text(client)["id"] == material["id"]
    assert len(list(tmp_path.glob("learning.sqlite3.before-v*.bak"))) == 1


def test_real_mcp_practice_and_message_tokens_obey_selection(learning_mcp):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from backend.app.learning import create_learning_router

    url, path = learning_mcp
    app = create_app(path, seed_demo=False, include_ai=False, frontend_dist=path.parent / "missing")

    def reply(messages, info):
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"answer": TEXT, "citation_ids": []})]
        )

    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=url,
            model_factory=lambda: FunctionModel(reply),
        )
    )

    async def practice():
        async with (
            httpx.AsyncClient(headers=mcp_headers()) as http,
            streamable_http_client(url, http_client=http) as (read, write, _),
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_practice_context", {"language": "en"})
                assert not result.isError
                return json.loads(result.content[0].text)

    with TestClient(app, base_url="http://localhost") as client:
        material = import_text(client)
        for surface in ("Hmmm", "John", "wooow", "hello"):
            pick(client, material, token_for(material, surface))
        assert {word["lemma"] for word in asyncio.run(practice())["words"]} == {"hello"}
        conversation = client.post("/api/conversations", json={}).json()["id"]
        endpoint = f"/api/conversations/{conversation}/messages"
        response = client.post(
            endpoint, json={"message": "Say hello.", "request_id": "selection-reply"}
        )
        assert response.status_code == 200, response.text
        turn = client.get(f"/api/conversations/{conversation}").json()["messages"][0]
        assert turn["response"]["answer"] == TEXT
        assert {
            t["surface"].casefold() for t in turn["tokens"] if t["vocabulary"]["eligible"]
        } == ALLOWED
        hidden = next(t for t in turn["tokens"] if t["surface"] == "Hmmm")
        selection = client.post(
            f"{endpoint}/selection-reply/words/{hidden['id']}/selection",
            json={"request_id": "hidden-message-word"},
        )
        assert selection.status_code == 200, selection.text
        assert {word["lemma"] for word in asyncio.run(practice())["words"]} == {"hello"}
        client.put(f"/api/lexemes/{hidden['lexeme_id']}/knowledge", json={"score": 1})
        assert {word["lemma"] for word in asyncio.run(practice())["words"]} == {"hello", "hmmm"}
        assert client.delete(f"/api/conversations/{conversation}").status_code == 200
        assert (
            client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"}).status_code
            == 200
        )
        assert asyncio.run(practice())["words"] == []
