"""Progress agrees with saved knowledge and actual elapsed playback."""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from backend.app.main import create_learner_app as create_app


def client_for(path):
    return TestClient(
        create_app(path, seed_demo=False, include_ai=False), base_url="http://localhost"
    )


def add_material(client):
    response = client.post(
        "/api/materials",
        json={
            "youtube_url": "https://youtu.be/TLkA0RELQ1g",
            "title": "Progress example",
            "content": "1\n00:00:00,000 --> 00:00:03,000\nWe watch films.\n\n"
            "2\n00:00:04,000 --> 00:00:07,000\nWe watched films.\n",
            "format": "srt",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def progress(client, **params):
    response = client.get("/api/progress", params={"timezone": "Europe/Moscow", **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_automatic_and_manual_scores_share_counts_without_duplicate_words(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with client_for(path) as client:
        material = add_material(client)
        assert progress(client)["counts"]["known"] == 0
        client.put("/api/settings/learning", json={"enabled": True})
        viewing = client.post(f"/api/materials/{material['id']}/viewing", json={}).json()[
            "viewing_id"
        ]
        heard = client.post(
            f"/api/viewings/{viewing}/cues",
            json={
                "cue_ids": [cue["id"] for cue in material["cues"]],
            },
        )
        assert heard.status_code == 200
        assert progress(client)["counts"]["known"] == 0
        assert client.post(f"/api/viewings/{viewing}/confirm", json={}).status_code == 200
        result = progress(client, days=30)
        assert result["counts"]["known"] == 3
        assert result["known_change"] == 3
        assert result["series"][-1]["known_change"] == 3
        assert client.post(f"/api/viewings/{viewing}/confirm", json={}).status_code == 200
        assert progress(client)["known_change"] == 3
        watch = next(token for token in material["cues"][0]["tokens"] if token["lemma"] == "watch")
        client.put(f"/api/lexemes/{watch['lexeme_id']}/knowledge", json={"score": 3})
        promoted = progress(client)
        assert promoted["counts"]["known"] == 3
        assert promoted["counts"]["confident"] == promoted["confident_change"] == 1
        client.put(
            f"/api/lexemes/{watch['lexeme_id']}/knowledge", json={"score": 3, "ignored": True}
        )
        assert progress(client)["counts"]["known"] == 2
        client.put(
            f"/api/lexemes/{watch['lexeme_id']}/knowledge", json={"score": 1, "ignored": False}
        )
        assert progress(client)["counts"]["1"] == 1
        day = client.get(
            "/api/progress/day",
            params={
                "date": result["today"],
                "timezone": "Europe/Moscow",
            },
        ).json()
        assert len(day["words"]) == 6
        assert any(word["data"].get("reason") == "viewing_summary" for word in day["words"])
        assert day["materials"] == []
    with client_for(path) as client:
        assert progress(client)["counts"]["known"] == 2
        assert progress(client)["known_change"] == 2


def test_existing_diagnostic_and_prior_words_are_a_baseline(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with client_for(path) as client:
        material = add_material(client)
        token = material["cues"][0]["tokens"][1]
        with client.app.state.materials.connect() as db:
            db.execute(
                "INSERT INTO word_knowledge VALUES(?,?,0,'diagnostic',1,?)",
                (token["lexeme_id"], 2, datetime.now(UTC).isoformat()),
            )
            db.execute("DELETE FROM progress_vocabulary")
    with client_for(path) as client:
        result = progress(client)
        assert result["counts"]["known"] == result["counts"]["provisional"] == 1
        assert result["known_change"] == 0
        assert result["series"][-1]["known"] == 1
        assert result["series"][-1]["known_change"] == 0
        assert all(day["known"] is None for day in result["series"][:-1])
        # A diagnostic inference is included even when it has no explicit saved word row.
        with client.app.state.materials.connect() as db:
            db.execute("INSERT INTO diagnostic_rules VALUES('en',1,1,1)")
        result = progress(client)
        dictionary = client.get("/api/dictionary").json()
        expected = sum(word["knowledge"]["score"] in {2, 3} for word in dictionary)
        assert result["counts"]["known"] == expected > 1


def test_playback_is_idempotent_unions_tabs_and_survives_restart_and_reset(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with client_for(path) as client:
        material = add_material(client)
        generation = client.get("/api/profile").json()["storage_generation"]
        start = datetime.now(UTC) - timedelta(minutes=5)
        payload = {
            "material_id": material["id"],
            "generation": generation,
            "intervals": [
                {
                    "id": "first-interval",
                    "started_at": start.isoformat(),
                    "ended_at": (start + timedelta(seconds=20)).isoformat(),
                },
                {
                    "id": "second-interval",
                    "started_at": (start + timedelta(seconds=10)).isoformat(),
                    "ended_at": (start + timedelta(seconds=30)).isoformat(),
                },
            ],
        }
        assert client.get("/api/settings/learning").json()["enabled"] is False
        assert client.post("/api/progress/playback", json=payload).json() == {"saved": 2}
        assert client.post("/api/progress/playback", json=payload).json() == {"saved": 0}
        result = progress(client)
        assert result["watched_ms"] == 30_000
        assert result["active_days"] == 1
        assert result["counts"]["known"] == 0
        day = client.get(
            "/api/progress/day",
            params={
                "date": start.astimezone(ZoneInfo("Europe/Moscow")).date(),
                "timezone": "Europe/Moscow",
            },
        ).json()
        assert day["watched_ms"] == day["materials"][0]["watched_ms"] == 30_000
        changed = {
            **payload,
            "intervals": [{**payload["intervals"][0], "ended_at": start.isoformat()}],
        }
        assert client.post("/api/progress/playback", json=changed).status_code == 422
        changed["intervals"][0]["ended_at"] = (start + timedelta(seconds=25)).isoformat()
        assert client.post("/api/progress/playback", json=changed).status_code == 409
    with client_for(path) as client:
        assert progress(client)["watched_ms"] == 30_000
        client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"})
        assert progress(client)["watched_ms"] == 0
        assert progress(client)["counts"]["known"] == 0
        assert client.post("/api/progress/playback", json=payload).status_code == 409


def test_timezone_midnight_splits_playback_and_vocabulary_history(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        material = add_material(client)
        # Seed past timestamps to test the calendar independently of today's clock.
        with client.app.state.materials.connect() as db:
            today = datetime.now(UTC).date()
            stamp = datetime.combine(today - timedelta(days=2), datetime.min.time(), UTC)
            crossing = stamp.replace(hour=20, minute=59, second=50)
            db.execute(
                "INSERT INTO playback_intervals VALUES(?,?,?,?)",
                (
                    "midnight-playback",
                    material["id"],
                    crossing.isoformat(),
                    (crossing + timedelta(seconds=20)).isoformat(),
                ),
            )
            db.execute("UPDATE progress_vocabulary SET occurred_at=?", (stamp.isoformat(),))
            initial = json.loads(
                db.execute("SELECT counts_json FROM progress_vocabulary").fetchone()[0]
            )
            initial.update({"2": 1, "known": 1})
            db.execute(
                "INSERT INTO progress_vocabulary(occurred_at,reason,counts_json) VALUES(?,?,?)",
                ((crossing + timedelta(seconds=15)).isoformat(), "learning", json.dumps(initial)),
            )
            token = material["cues"][0]["tokens"][0]
            db.execute(
                "INSERT INTO word_knowledge VALUES(?,?,0,'manual',NULL,?)",
                (token["lexeme_id"], 2, crossing.isoformat()),
            )
        local = progress(client)
        days = {day["date"]: day for day in local["series"]}
        before, after = (stamp.date().isoformat(), (stamp.date() + timedelta(days=1)).isoformat())
        assert days[before]["watched_ms"] == days[after]["watched_ms"] == 10_000
        assert days[before]["known_change"] == 0
        assert days[after]["known_change"] == 1
        utc = progress(client, timezone="UTC")
        utc_days = {day["date"]: day for day in utc["series"]}
        assert utc_days[before]["watched_ms"] == 20_000
        assert utc_days[before]["known_change"] == 1
        for params in ({"days": 8}, {"timezone": "Missing/Timezone"}):
            assert client.get("/api/progress", params=params).status_code == 422
        for period in (7, 30, 90, 365):
            result = progress(client, days=period)
            assert len(result["series"]) == result["days"] == period
