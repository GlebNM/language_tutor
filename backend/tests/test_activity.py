"""Observable learning activity through public operations, without model access."""

import asyncio
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from backend.app.main import create_learner_app as create_app


def client_for(path: Path) -> TestClient:
    return TestClient(
        create_app(path, seed_demo=False, include_ai=False), base_url="http://localhost"
    )


def add_selection(client: TestClient, text: str = "She gave up smoking.") -> dict[str, str]:
    material = client.post(
        "/api/materials",
        json={
            "youtube_url": "https://youtu.be/TLkA0RELQ1g",
            "title": "Learning activity",
            "content": f"1\n00:00:01,000 --> 00:00:04,000\n{text}\n",
            "format": "srt",
        },
    ).json()
    cue = material["cues"][0]
    return {
        "material_id": material["id"],
        "cue_id": cue["id"],
        "token_id": cue["tokens"][1]["id"],
    }


def test_clicks_without_ai_are_persistent_and_reads_do_not_record_clicks(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with client_for(path) as client:
        assert client.get("/api/activity").json()["events"] == []
        selected = add_selection(client)
        material = client.get(f"/api/materials/{selected['material_id']}").json()
        before = material["cues"][0]["tokens"][1]["knowledge"]
        assert (
            client.get(
                f"/api/materials/{selected['material_id']}/selection",
                params={key: selected[key] for key in ("cue_id", "token_id")},
            ).status_code
            == 200
        )
        assert client.get("/api/activity").json()["events"] == []
        click = client.post(
            "/api/activity/word-selections",
            json={
                **selected,
                "request_id": "first-click-123",
            },
        )
        assert click.status_code == 200, click.text
        assert click.json()["token"]["knowledge"] == before
        event = client.get("/api/activity").json()["events"][0]
        assert event["id"] == click.json()["activity_id"]
        assert event["event_type"] == "word_selected"
        assert event["schema_version"] == 1
        assert event["word"] == {
            "language": "en",
            "lemma": "give",
            "part_of_speech": "VERB",
            "surface": "gave",
        }
        assert event["context"]["text"] == "She gave up smoking."
        assert event["occurred_at"].endswith("+00:00")
        assert event["data"] == {}
    with client_for(path) as client:
        assert client.get("/api/activity").json()["events"] == [event]
        assert client.get(f"/api/materials/{selected['material_id']}").json() == material


async def test_concurrent_delivery_is_idempotent_and_new_clicks_are_distinct(tmp_path):
    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    with TestClient(app, base_url="http://localhost") as client:
        first = add_selection(client)
        second = add_selection(client, "He watched the film.")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        payload = {**first, "request_id": "same-click-123"}
        replies = await asyncio.gather(
            *[client.post("/api/activity/word-selections", json=payload) for _ in range(3)]
        )
        assert all(reply.status_code == 200 for reply in replies)
        assert len({reply.json()["activity_id"] for reply in replies}) == 1
        assert len((await client.get("/api/activity")).json()["events"]) == 1
        conflict = await client.post(
            "/api/activity/word-selections",
            json={
                **second,
                "request_id": payload["request_id"],
            },
        )
        assert conflict.status_code == 409
        fresh = await client.post(
            "/api/activity/word-selections",
            json={
                **first,
                "request_id": "new-click-456",
            },
        )
        assert fresh.json()["activity_id"] != replies[0].json()["activity_id"]
        events = (await client.get("/api/activity")).json()["events"]
        assert len(events) == 2


def test_history_filters_pagination_and_invalid_inputs(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        first = add_selection(client)
        second = add_selection(client, "He watched the film.")
        for selected in (first, second, first):
            assert client.post("/api/activity/word-selections", json=selected).status_code == 200
        all_events = client.get("/api/activity").json()["events"]
        assert [event["material_id"] for event in all_events] == [
            first["material_id"],
            second["material_id"],
            first["material_id"],
        ]
        page = client.get("/api/activity", params={"limit": 2}).json()
        assert page["events"] == all_events[:2]
        assert page["has_more"] is True
        # A new click cannot shift or duplicate a page fetched with the returned cursor.
        client.post("/api/activity/word-selections", json=second)
        tail = client.get("/api/activity", params={"before_id": page["next_before_id"]}).json()
        assert tail["events"] == all_events[2:]
        assert tail["has_more"] is False
        assert tail["next_before_id"] is None
        assert (
            len(
                client.get(
                    "/api/activity",
                    params={
                        "material_id": first["material_id"],
                        "lexeme_id": all_events[0]["lexeme_id"],
                        "event_type": "word_selected",
                    },
                ).json()["events"]
            )
            == 2
        )
        assert (
            client.get(
                "/api/activity",
                params={
                    "event_type": "translation_received",
                },
            ).json()["events"]
            == []
        )
        assert (
            len(
                client.get(
                    "/api/activity",
                    params={
                        "since": all_events[0]["occurred_at"],
                    },
                ).json()["events"]
            )
            == 2
        )
        for params in (
            {"limit": 0},
            {"limit": 101},
            {"before_id": 0},
            {"event_type": "arbitrary"},
            {"since": "2026-10-01T00:00:00"},
            {"language": "ru"},
        ):
            assert client.get("/api/activity", params=params).status_code == 422
        assert (
            client.post(
                "/api/activity/word-selections",
                json={
                    **first,
                    "cue_id": second["cue_id"],
                },
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/activity/word-selections",
                json={
                    **first,
                    "translation": "client supplied text",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/activity/word-selections",
                json={
                    **first,
                    "request_id": "short",
                },
            ).status_code
            == 422
        )


def test_manual_changes_and_word_snapshots_survive_lexeme_correction(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        selected = add_selection(client)
        response = client.post("/api/activity/word-selections", json=selected).json()
        lexeme_id = response["token"]["lexeme_id"]
        path = f"/api/lexemes/{lexeme_id}/knowledge"
        for _ in range(2):
            assert client.put(path, json={"score": 1, "ignored": True}).status_code == 200
        events = client.get("/api/activity").json()["events"]
        assert [event["event_type"] for event in events] == ["knowledge_changed", "word_selected"]
        assert events[0]["data"]["previous"]["score"] is None
        assert events[0]["data"]["current"]["score"] == 1
        assert events[0]["data"]["current"]["ignored"] is True
        corrected = client.put(
            f"/api/materials/{selected['material_id']}/occurrences/{selected['token_id']}/lexeme",
            json={"cue_id": selected["cue_id"], "lemma": "gave", "part_of_speech": "NOUN"},
        )
        assert corrected.status_code == 200, corrected.text
        updated = client.get("/api/activity").json()["events"]
        assert updated[0]["event_type"] == "lexeme_corrected"
        assert updated[0]["word"]["lemma"] == "gave"
        assert updated[0]["data"]["previous_word"]["lexeme_id"] == lexeme_id
        assert updated[1:] == events
        assert (
            client.get("/api/activity", params={"lexeme_id": lexeme_id}).json()["events"] == events
        )


def test_reset_clears_activity_without_restoring_it_on_restart(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with client_for(path) as client:
        selected = add_selection(client)
        client.post("/api/activity/word-selections", json=selected)
        assert client.get("/api/activity").json()["events"]
        assert (
            client.post(
                "/api/data/reset",
                json={
                    "confirmation": "Удалить мои данные",
                },
            ).status_code
            == 200
        )
        assert client.get("/api/activity").json()["events"] == []
        assert client.post("/api/activity/word-selections", json=selected).status_code == 404
    with client_for(path) as client:
        assert client.get("/api/activity").json()["events"] == []


def test_ai_failure_keeps_click_without_claiming_a_received_translation(tmp_path):
    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False)
    with TestClient(app, base_url="http://localhost") as client:
        selected = add_selection(client)
        assert client.post("/api/activity/word-selections", json=selected).status_code == 200
        assert client.post("/api/cards", json=selected).status_code == 409
        events = client.get("/api/activity").json()["events"]
        assert [event["event_type"] for event in events] == ["word_selected"]
