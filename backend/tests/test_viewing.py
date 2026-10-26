"""Reviewed vocabulary batches through public API operations."""

import asyncio
import sqlite3

import httpx
from fastapi.testclient import TestClient

from backend.app import materials as material_storage
from backend.app.main import create_learner_app as create_app


def make_app(path):
    return create_app(
        path, seed_demo=False, include_ai=False, frontend_dist=path.parent / "missing"
    )


def add_material(client):
    response = client.post(
        "/api/materials",
        json={
            "youtube_url": "https://youtu.be/TLkA0RELQ1g",
            "title": "Viewing vocabulary",
            "format": "srt",
            "content": (
                "1\n00:00:00,000 --> 00:00:03,000\nWe watch films.\n\n"
                "2\n00:00:04,000 --> 00:00:07,000\nThey watched birds.\n\n"
                "3\n00:00:08,000 --> 00:00:11,000\nA machine ticks.\n"
            ),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def resume(client, material):
    response = client.post(f"/api/materials/{material['id']}/viewing", json={})
    assert response.status_code == 200, response.text
    return response.json()["viewing_id"]


def click(client, material, cue, token, viewing_id=None):
    body = {"material_id": material["id"], "cue_id": cue["id"], "token_id": token["id"]}
    if viewing_id:
        body["viewing_id"] = viewing_id
    response = client.post("/api/activity/word-selections", json=body)
    assert response.status_code == 200, response.text


def summary(client, viewing_id):
    response = client.get(f"/api/viewings/{viewing_id}")
    assert response.status_code == 200, response.text
    return response.json()["entries"]


def test_prepare_only_listened_cues_and_live_clicks_without_changing_knowledge(tmp_path):
    with TestClient(make_app(tmp_path / "learning.sqlite3"), base_url="http://localhost") as client:
        material = add_material(client)
        assert client.get("/api/settings/learning").json()["enabled"] is False
        assert resume(client, material) is None
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = resume(client, material)
        assert resume(client, material) == viewing
        first, second, third = material["cues"]
        # Other surfaces still record a selection, but do not propose familiarity.
        click(client, material, third, third["tokens"][1])
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [first["id"], first["id"]]})
        # A click in a cue not fully heard still proposes unknown for that word only.
        click(client, material, second, second["tokens"][1], viewing)
        entries = summary(client, viewing)
        assert {entry["lemma"]: entry["score"] for entry in entries} == {
            "we": 2,
            "watch": 0,
            "film": 2,
        }
        assert client.get(f"/api/materials/{material['id']}").json() == material
        assert client.get("/api/dictionary").json() == []
        listened = client.get("/api/activity", params={"event_type": "cue_listened"}).json()
        assert len(listened["events"]) == 1
        assert listened["events"][0]["context"]["text"] == first["text"]


def test_edit_exclude_close_and_resume_then_confirm_persists(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with TestClient(make_app(path), base_url="http://localhost") as client:
        material = add_material(client)
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = resume(client, material)
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [material["cues"][0]["id"]]})
        entries = {entry["lemma"]: entry for entry in summary(client, viewing)}
        for lemma, score, included in (("we", 1, True), ("film", 2, False)):
            response = client.put(
                f"/api/viewings/{viewing}/choice",
                json={
                    "lexeme_id": entries[lemma]["lexeme_id"],
                    "score": score,
                    "included": included,
                },
            )
            assert response.status_code == 200, response.text
        # Reading/editing/closing a summary does not apply knowledge.
        assert client.get("/api/dictionary").json() == []
    with TestClient(make_app(path), base_url="http://localhost") as client:
        assert resume(client, material) == viewing
        entries = {entry["lemma"]: entry for entry in summary(client, viewing)}
        assert entries["we"]["score"] == 1
        assert entries["film"]["included"] is False
        result = client.post(f"/api/viewings/{viewing}/confirm", json={})
        assert result.json()["saved"] == 2
        assert client.post(f"/api/viewings/{viewing}/confirm", json={}).json() == result.json()
        saved = client.get("/api/dictionary").json()
        assert {word["lemma"]: word["knowledge"]["score"] for word in saved} == {
            "we": 1,
            "watch": 2,
        }
        assert all(word["knowledge"]["source"] == "manual" for word in saved)
        events = client.get("/api/activity", params={"event_type": "knowledge_changed"}).json()[
            "events"
        ]
        assert len(events) == 2
        assert all(event["data"]["reason"] == "viewing_summary" for event in events)
        assert (
            next(event for event in events if event["word"]["lemma"] == "we")["data"][
                "suggested_score"
            ]
            == 2
        )
        new_viewing = resume(client, material)
        assert new_viewing != viewing
        client.post(
            f"/api/viewings/{new_viewing}/cues", json={"cue_ids": [material["cues"][0]["id"]]}
        )
        assert [entry["lemma"] for entry in summary(client, new_viewing)] == ["film"]


def test_protect_existing_and_concurrent_manual_assessments(tmp_path):
    with TestClient(make_app(tmp_path / "learning.sqlite3"), base_url="http://localhost") as client:
        material = add_material(client)
        cue = material["cues"][0]
        we, watch, films = cue["tokens"]
        client.put(f"/api/lexemes/{we['lexeme_id']}/knowledge", json={"score": None})
        client.put(
            f"/api/lexemes/{films['lexeme_id']}/knowledge", json={"score": 0, "ignored": True}
        )
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = resume(client, material)
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [cue["id"]]})
        assert [entry["lemma"] for entry in summary(client, viewing)] == ["watch"]
        # A manual change made after displaying the summary wins at confirmation too.
        client.put(f"/api/lexemes/{watch['lexeme_id']}/knowledge", json={"score": 1})
        assert client.post(f"/api/viewings/{viewing}/confirm", json={}).json()["saved"] == 0
        saved = client.get("/api/dictionary").json()
        assert {word["lemma"]: word["knowledge"]["score"] for word in saved} == {
            "we": None,
            "watch": 1,
            "film": 0,
        }
        assert (
            next(word for word in saved if word["lemma"] == "film")["knowledge"]["ignored"] is True
        )


def test_disable_clears_all_pending_materials_and_stale_requests_cannot_restore_them(tmp_path):
    with TestClient(make_app(tmp_path / "learning.sqlite3"), base_url="http://localhost") as client:
        material = add_material(client)
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = resume(client, material)
        cue = material["cues"][0]
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [cue["id"]]})
        client.put("/api/settings/learning", json={"enabled": False})
        client.put("/api/settings/learning", json={"enabled": True})
        assert client.post(f"/api/viewings/{viewing}/confirm", json={}).status_code == 409
        assert (
            client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [cue["id"]]}).status_code
            == 409
        )
        click(client, material, cue, cue["tokens"][1], viewing)
        new_viewing = resume(client, material)
        assert new_viewing != viewing
        assert summary(client, new_viewing) == []
        assert client.get("/api/dictionary").json() == []
        assert (
            len(client.get("/api/activity", params={"event_type": "cue_listened"}).json()["events"])
            == 1
        )


def test_foreign_cues_and_choices_are_rejected_atomically_and_reset_clears_viewings(tmp_path):
    with TestClient(make_app(tmp_path / "learning.sqlite3"), base_url="http://localhost") as client:
        material = add_material(client)
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = resume(client, material)
        cue = material["cues"][0]
        response = client.post(
            f"/api/viewings/{viewing}/cues", json={"cue_ids": [cue["id"], "foreign"]}
        )
        assert response.status_code == 404
        assert summary(client, viewing) == []
        assert client.get("/api/activity").json()["events"] == []
        assert (
            client.put(
                f"/api/viewings/{viewing}/choice",
                json={
                    "lexeme_id": cue["tokens"][0]["lexeme_id"],
                    "score": 3,
                },
            ).status_code
            == 409
        )
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [cue["id"]]})
        assert (
            client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"}).status_code
            == 200
        )
        assert client.get("/api/settings/learning").json()["enabled"] is False
        assert client.get("/api/activity").json()["events"] == []
        assert client.post(f"/api/viewings/{viewing}/confirm", json={}).status_code == 409


async def test_concurrent_confirmation_records_one_batch(tmp_path):
    app = make_app(tmp_path / "learning.sqlite3")
    with TestClient(app, base_url="http://localhost") as client:
        material = add_material(client)
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = resume(client, material)
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [material["cues"][0]["id"]]})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        replies = await asyncio.gather(
            *[client.post(f"/api/viewings/{viewing}/confirm", json={}) for _ in range(3)]
        )
        assert all(reply.status_code == 200 for reply in replies)
        assert all(reply.json() == replies[0].json() for reply in replies)
        assert len((await client.get("/api/dictionary")).json()) == 3
        assert (
            len(
                (
                    await client.get("/api/activity", params={"event_type": "viewing_confirmed"})
                ).json()["events"]
            )
            == 1
        )


def test_click_retry_keeps_its_viewing_and_cannot_move_to_another_batch(tmp_path):
    with TestClient(make_app(tmp_path / "learning.sqlite3"), base_url="http://localhost") as client:
        material = add_material(client)
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = resume(client, material)
        cue = material["cues"][0]
        payload = {
            "material_id": material["id"],
            "cue_id": cue["id"],
            "token_id": cue["tokens"][0]["id"],
            "viewing_id": viewing,
            "request_id": "same-viewing-click",
        }
        first = client.post("/api/activity/word-selections", json=payload)
        assert first.status_code == 200
        assert client.post("/api/activity/word-selections", json=payload).json() == first.json()
        client.post(f"/api/viewings/{viewing}/confirm", json={})
        payload["viewing_id"] = resume(client, material)
        assert client.post("/api/activity/word-selections", json=payload).status_code == 409
        assert summary(client, payload["viewing_id"]) == []


def test_upgrade_preserves_existing_profile_dictionary_activity_and_backup(tmp_path):
    path = tmp_path / "learning.sqlite3"
    # Seed through current public operations, then restore the pre-viewing schema.
    # Running current API code against a deliberately incomplete schema is invalid.
    with TestClient(make_app(path), base_url="http://localhost") as client:
        material = add_material(client)
        cue = material["cues"][0]
        client.put(f"/api/lexemes/{cue['tokens'][0]['lexeme_id']}/knowledge", json={"score": 1})
        click(client, material, cue, cue["tokens"][1])
        expected = {
            route: client.get(route).json()
            for route in ("/api/profile", "/api/dictionary", "/api/activity")
        }
    version = next(
        i
        for i, script in enumerate(material_storage.MIGRATIONS)
        if "CREATE TABLE learning_settings" in script
    )
    with sqlite3.connect(path) as db:
        for table in (
            "expression_events",
            "expression_jobs",
            "expression_occurrences",
            "expression_knowledge",
            "expressions",
            "playback_intervals",
            "progress_vocabulary",
            "subtitle_improvements",
            "occurrence_vocabulary",
            "message_vocabulary",
            "viewing_choices",
            "viewing_clicks",
            "viewing_cues",
            "viewings",
            "learning_settings",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("DELETE FROM storage_metadata WHERE key='playback_tracking_since'")
        db.execute(f"PRAGMA user_version={version}")
    with TestClient(make_app(path), base_url="http://localhost") as client:
        assert {route: client.get(route).json() for route in expected} == expected
        assert client.get("/api/settings/learning").json()["enabled"] is False
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = resume(client, material)
        client.post(f"/api/viewings/{viewing}/cues", json={"cue_ids": [cue["id"]]})
        assert {entry["lemma"] for entry in summary(client, viewing)} == {"watch", "film"}
    assert len(list(tmp_path.glob("learning.sqlite3.before-v*.bak"))) == 1
