import pytest
from fastapi.testclient import TestClient

from backend.app.knowledge import create_knowledge_router
from backend.app.main import create_app


def client_for(path):
    app = create_app(
        path, seed_demo=False, include_ai=False, frontend_dist=path.parent / "missing-dist"
    )
    app.include_router(create_knowledge_router(app.state.materials))
    return TestClient(app, base_url="http://localhost:8765")


def import_text(client, text, **changes):
    response = client.post(
        "/api/materials",
        json={
            "youtube_url": "https://youtu.be/TLkA0RELQ1g",
            "title": "Vocabulary",
            "format": "srt",
            "content": f"1\n00:00:00,000 --> 00:00:10,000\n{text}\n",
            **changes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def token_for(material, surface):
    return next(t for c in material["cues"] for t in c["tokens"] if t["surface"] == surface)


def test_manual_word_knowledge_updates_forms_and_survives_restart(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with client_for(path) as client:
        material = import_text(
            client, "We watch films. They watched films. We are watching films. A watch ticks."
        )
        watch = token_for(material, "watched")
        response = client.put(f"/api/lexemes/{watch['lexeme_id']}/knowledge", json={"score": 1})
        assert response.status_code == 200, response.text
        reopened = client.get(f"/api/materials/{material['id']}").json()
        verbs = [
            t
            for c in reopened["cues"]
            for t in c["tokens"]
            if t["lemma"] == "watch" and t["part_of_speech"] == "VERB"
        ]
        assert len(verbs) == 3
        assert all(
            t["knowledge"]["score"] == 1 and t["knowledge"]["source"] == "manual" for t in verbs
        )
        noun = next(
            t
            for c in reopened["cues"]
            for t in c["tokens"]
            if t["lemma"] == "watch" and t["part_of_speech"] == "NOUN"
        )
        assert noun["knowledge"]["score"] is None
        assert (
            client.get("/api/dictionary", params={"search": "wat", "score": "1"}).json()[0][
                "lexeme_id"
            ]
            == watch["lexeme_id"]
        )
    with client_for(path) as client:
        assert client.get(f"/api/materials/{material['id']}").json() == reopened
        assert (
            client.put(
                f"/api/lexemes/{watch['lexeme_id']}/knowledge",
                json={"score": None, "ignored": True},
            ).status_code
            == 200
        )
        saved = client.get(
            "/api/dictionary", params={"ignored": True, "score": "unassessed"}
        ).json()
        assert saved[0]["knowledge"] == {
            "score": None,
            "ignored": True,
            "source": "manual",
            "provisional": False,
            "status": "Исключено",
        }
        assert (
            client.put(
                f"/api/lexemes/{watch['lexeme_id']}/knowledge",
                json={"score": None, "ignored": False},
            ).json()["status"]
            == "Пока не оценено"
        )


def test_correcting_one_occurrence_retains_independent_lexical_assessments(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        material = import_text(client, "We watch a watch and we watch again.")
        cue = material["cues"][0]
        watches = [t for t in cue["tokens"] if t["surface"] == "watch"]
        verb, noun, repeated = watches
        client.put(f"/api/lexemes/{verb['lexeme_id']}/knowledge", json={"score": 3})
        client.put(f"/api/lexemes/{noun['lexeme_id']}/knowledge", json={"score": 0})
        response = client.put(
            f"/api/materials/{material['id']}/occurrences/{verb['id']}/lexeme",
            json={"cue_id": cue["id"], "lemma": "watch", "part_of_speech": "NOUN"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["token"]["knowledge"]["score"] == 0
        reopened = client.get(f"/api/materials/{material['id']}").json()
        assert token_for(reopened, "again")["knowledge"]["score"] is None
        untouched = next(t for t in reopened["cues"][0]["tokens"] if t["id"] == repeated["id"])
        assert untouched["knowledge"]["score"] == 3
        assert (
            client.put(
                f"/api/materials/{material['id']}/occurrences/{verb['id']}/lexeme",
                json={"cue_id": "wrong", "lemma": "watch", "part_of_speech": "NOUN"},
            ).status_code
            == 404
        )


def test_difficulty_counts_spoken_repetitions_and_excludes_ignored_numbers_markers(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        material = import_text(client, "[Music] We watch films. We watch films. 123 456.")
        for surface, score, ignored in [
            ("We", 3, False),
            ("watch", 1, False),
            ("films", None, False),
        ]:
            token = token_for(material, surface)
            client.put(
                f"/api/lexemes/{token['lexeme_id']}/knowledge",
                json={"score": score, "ignored": ignored},
            )
        result = client.get(f"/api/materials/{material['id']}/difficulty").json()
        assert (
            result["total"],
            result["known"],
            result["unfamiliar"],
            result["unassessed"],
            result["unique_unfamiliar"],
        ) == (6, 2, 2, 2, 1)
        assert result["coverage_interval"] == [100 / 3, 200 / 3]
        assert result["label"] is None
        films = token_for(material, "films")
        client.put(
            f"/api/lexemes/{films['lexeme_id']}/knowledge", json={"score": None, "ignored": True}
        )
        result = client.get(f"/api/materials/{material['id']}/difficulty").json()
        assert (result["total"], result["known_percent"], result["unassessed_percent"]) == (
            4,
            50,
            0,
        )


@pytest.mark.parametrize(
    ("known", "unknown", "unassessed", "complete", "label"),
    [
        (95, 5, 0, True, "Мало новой лексики"),
        (94, 6, 0, True, "Понадобятся подсказки"),
        (90, 10, 0, True, "Понадобятся подсказки"),
        (89, 11, 0, True, "Много новой лексики"),
        (80, 0, 20, True, "Много новой лексики"),
        (79, 0, 21, True, None),
        (50, 0, 0, False, None),
        (49, 0, 0, True, None),
    ],
)
def test_difficulty_labels_obey_coverage_boundaries(
    tmp_path, known, unknown, unassessed, complete, label
):
    with client_for(tmp_path / "learning.sqlite3") as client:
        material = import_text(
            client,
            " ".join(["cat"] * known + ["dog"] * unknown + ["horse"] * unassessed),
            is_complete=complete,
        )
        for surface, score in [("cat", 2), ("dog", 0)]:
            if surface == "dog" and not unknown:
                continue
            token = token_for(material, surface)
            client.put(f"/api/lexemes/{token['lexeme_id']}/knowledge", json={"score": score})
        result = client.get(f"/api/materials/{material['id']}/difficulty").json()
        assert result["label"] == label


def test_empty_lexical_track_has_no_percentages(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        material = import_text(client, "123 [Music] --")
        result = client.get(f"/api/materials/{material['id']}/difficulty").json()
        assert result["total"] == 0
        assert result["known_percent"] is result["coverage_interval"] is result["label"] is None


def test_invalid_familiarity_does_not_change_dictionary(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        material = import_text(client, "We watch films.")
        token = token_for(material, "watch")
        for score in [True, 4, -1, "2"]:
            assert (
                client.put(
                    f"/api/lexemes/{token['lexeme_id']}/knowledge", json={"score": score}
                ).status_code
                == 422
            )
        assert client.get("/api/dictionary").json() == []


def test_schema_upgrade_preserves_a_restorable_backup(tmp_path):
    import shutil
    import sqlite3
    from pathlib import Path

    database = tmp_path / "learning.sqlite3"
    with sqlite3.connect(database) as legacy:
        legacy.executescript((Path(__file__).parent / "fixtures/learning-v1.sql").read_text())
        legacy.execute(
            "INSERT INTO materials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-material",
                "TLkA0RELQ1g",
                "Saved before upgrade",
                "en",
                "srt",
                1,
                "fixture-hash",
                1,
                None,
                "legacy-v1",
                "2026-10-01",
            ),
        )
        legacy.execute(
            "INSERT INTO cues VALUES(?,?,?,?,?,?,?,?)",
            ("legacy-cue", "legacy-material", 0, 0, 1000, "Watch.", "Watch.", '["Watch."]'),
        )
        legacy.execute(
            "INSERT INTO lexemes VALUES(?,?,?,?)", ("legacy-word", "en", "watch", "VERB")
        )
        legacy.execute(
            "INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)",
            ("legacy-occurrence", "legacy-material", "legacy-cue", "legacy-word", 0, 5, "Watch", 0),
        )
    with client_for(database) as client:
        material = client.get("/api/materials/legacy-material").json()
        assert material["cues"][0]["text"] == "Watch."
        assert material["cues"][0]["tokens"][0]["knowledge"]["score"] is None
        assert (
            client.put("/api/lexemes/legacy-word/knowledge", json={"score": 3}).status_code == 200
        )
    backups = list(tmp_path.glob("learning.sqlite3.before-v*.bak"))
    assert len(backups) == 1
    restored = tmp_path / "restored.sqlite3"
    shutil.copyfile(backups[0], restored)
    with client_for(restored) as client:
        assert client.get("/api/materials/legacy-material").json() == material
    with client_for(database) as client:
        assert client.get("/api/dictionary").json()[0]["knowledge"]["score"] == 3
    assert list(tmp_path.glob("learning.sqlite3.before-v*.bak")) == backups


def test_api_and_mcp_can_initialize_the_same_database_concurrently(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from backend.app.materials import MaterialLibrary

    path = tmp_path / "learning.sqlite3"
    with ThreadPoolExecutor(max_workers=4) as workers:
        profiles = list(workers.map(lambda _: MaterialLibrary(path).get_profile(), range(4)))
    assert all(profile == profiles[0] for profile in profiles)


def test_reset_removes_owned_migration_backups_and_retains_unrelated_files(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with client_for(path) as client:
        import_text(client, "We watch films.")
        owned = tmp_path / (path.name + ".before-v1-" + "a" * 32 + ".bak")
        owned.write_text("Old private data")
        owned_wal = tmp_path / (owned.name + "-wal")
        owned_wal.write_text("Old backup pages")
        unrelated = tmp_path / "learning.sqlite3.before-v1-personal.bak"
        unrelated.write_text("Unrelated backup")
        response = client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"})
        assert response.status_code == 200, response.text
        assert not owned.exists()
        assert not owned_wal.exists()
        assert unrelated.read_text() == "Unrelated backup"
        assert client.get("/api/materials").json() == []
