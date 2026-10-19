import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app

SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\nThe children were running.\n\n"
    "2\n00:00:04,000 --> 00:00:06,000\nThey ran home.\n"
)


def payload(content=SRT, **changes):
    return {
        "youtube_url": "https://www.youtube.com/watch?v=TLkA0RELQ1g",
        "title": "Example",
        "format": "srt",
        "content": content,
        "is_complete": True,
        **changes,
    }


def test_import_can_be_reopened_after_restart_with_one_profile(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with TestClient(
        create_app(path, seed_demo=False, include_ai=False),
        base_url="http://localhost:8765",
    ) as client:
        profile = client.get("/api/profile").json()
        response = client.post("/api/materials", json=payload())
        assert response.status_code == 201, response.text
        material = response.json()
        assert material["cues"][0]["text"] == "The children were running."
        assert material["cues"][1]["start_ms"] == 4000
    with TestClient(
        create_app(path, seed_demo=False, include_ai=False),
        base_url="http://localhost:8765",
    ) as client:
        assert client.get("/api/profile").json() == profile
        assert client.get(f"/api/materials/{material['id']}").json() == material
        assert len(client.get("/api/materials").json()) == 1


def test_selection_has_real_offsets_and_lexical_identity_and_rejects_mixed_ids(
    tmp_path,
):
    with TestClient(
        create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False),
        base_url="http://localhost:8765",
    ) as client:
        first = client.post("/api/materials", json=payload()).json()
        second = client.post("/api/materials", json=payload(SRT.replace("home", "away"))).json()
        cue = first["cues"][0]
        running = next(token for token in cue["tokens"] if token["surface"] == "running")
        assert (running["lemma"], running["part_of_speech"]) == ("run", "VERB")
        assert cue["text"][running["start_offset"] : running["end_offset"]] == "running"
        ran = next(token for token in first["cues"][1]["tokens"] if token["surface"] == "ran")
        assert ran["lexeme_id"] == running["lexeme_id"]
        selected = client.get(
            f"/api/materials/{first['id']}/selection",
            params={"cue_id": cue["id"], "token_id": running["id"]},
        )
        assert selected.status_code == 200
        assert selected.json()["cue"]["start_ms"] == 1000
        for material_id, cue_id, token_id in [
            (second["id"], cue["id"], running["id"]),
            (first["id"], second["cues"][0]["id"], running["id"]),
            (first["id"], cue["id"], ran["id"]),
        ]:
            assert (
                client.get(
                    f"/api/materials/{material_id}/selection",
                    params={"cue_id": cue_id, "token_id": token_id},
                ).status_code
                == 404
            )


def test_local_api_rejects_foreign_origins_hosts_and_request_formats(tmp_path):
    with TestClient(
        create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False),
        base_url="http://localhost:8765",
    ) as client:
        assert (
            client.post(
                "/api/materials",
                json=payload(),
                headers={"Origin": "https://untrusted.example"},
            ).status_code
            == 403
        )
        assert client.get("/api/materials", headers={"Host": "attacker.example"}).status_code == 400
        assert (
            client.post(
                "/api/materials", content="{}", headers={"Content-Type": "text/plain"}
            ).status_code
            == 415
        )
        assert (
            client.post(
                "/api/materials",
                content='"' + "x" * 2_200_001 + '"',
                headers={"Content-Type": "application/json"},
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/api/materials",
                json=payload(),
                headers={"Origin": "http://localhost:8765"},
            ).status_code
            == 201
        )
        assert (
            client.post("/api/materials", json={**payload(), "unexpected": True}).status_code == 422
        )
        assert client.post("/api/materials", json=payload(is_complete="true")).status_code == 422


def test_import_reuses_hash_versions_replacements_and_normalizes_only_overlapping_speech(
    tmp_path,
):
    content = (
        "1\n00:00:00,000 --> 00:00:02,000\nWe can\n\n"
        "2\n00:00:01,000 --> 00:00:03,000\nWe can see.\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nWe can see.\n"
    )
    with TestClient(
        create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False),
        base_url="http://localhost:8765",
    ) as client:
        material = client.post("/api/materials", json=payload(content)).json()
        assert [cue["text"] for cue in material["cues"]] == [
            "We can see.",
            "We can see.",
        ]
        assert material["cues"][0]["start_ms"] == 0
        assert material["cues"][0]["end_ms"] == 3000
        assert material["cues"][0]["original_fragments"] == ["We can", "We can see."]
        repeated = client.post("/api/materials", json=payload(content)).json()
        assert repeated["id"] == material["id"]
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<i>😀 Children ran.</i>\n"
        replacement = client.put(
            f"/api/materials/{material['id']}/track",
            json=payload(vtt, format="vtt", is_complete=False),
        ).json()
        assert replacement["version"] == 2
        assert replacement["previous_version_id"] == material["id"]
        assert replacement["is_complete"] is False
        assert replacement["cues"][0]["original_text"] == "<i>😀 Children ran.</i>"
        assert replacement["cues"][0]["text"] == "😀 Children ran."
        assert replacement["cues"][0]["tokens"][0]["surface"] == "Children"
        assert replacement["cues"][0]["tokens"][0]["start_offset"] == 2
        assert client.get(f"/api/materials/{material['id']}").json() == material
        assert len(client.get("/api/materials").json()) == 2
        assert (
            client.put(
                f"/api/materials/{material['id']}/track",
                json=payload(vtt, format="vtt", youtube_url="https://youtu.be/YE7VzlLtp-4"),
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"content": ""}, "пуст"),
        ({"content": "1\n00:00:02,000 --> 00:00:01,000\nBackwards."}, "конец"),
        (
            {
                "content": (
                    "1\n00:01:00,000 --> 00:01:01,000\nFirst.\n\n"
                    "2\n00:00:00,000 --> 00:00:01,000\nSecond."
                )
            },
            "времени",
        ),
        ({"content": "1\n00:61:00,000 --> 00:62:00,000\nBad."}, "меньше 60"),
        ({"content": "1\n00:00:00,000 --> 00:00:01,000\n<i></i>"}, "пуст"),
        (
            {"content": "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHi", "format": "srt"},
            "время",
        ),
        ({"content": SRT, "format": "vtt"}, "WEBVTT"),
        (
            {"youtube_url": "https://youtube.com.attacker.example/watch?v=TLkA0RELQ1g"},
            "youtube.com",
        ),
        ({"content": "1\n00:00:00,000 --> 00:00:01,000\n" + "a" * 200_001}, "200 000"),
        ({"content": "1\n00:00:00,000 --> 00:00:01,000\n" + "a" * 2_000_001}, "2 МБ"),
        (
            {
                "content": "\n\n".join(
                    f"{index + 1}\n00:00:00,000 --> 00:00:01,000\na" for index in range(10_001)
                )
            },
            "10 000",
        ),
    ],
)
def test_invalid_import_is_explained_and_does_not_add_a_material(tmp_path, changes, message):
    with TestClient(
        create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False),
        base_url="http://localhost:8765",
    ) as client:
        response = client.post("/api/materials", json=payload(**changes))
        assert response.status_code == 422
        assert message in response.json()["detail"]
        assert client.get("/api/materials").json() == []


def test_vtt_metadata_and_uncertain_words_retain_readable_text(tmp_path):
    vtt = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "NOTE creator comment\nnot a cue\n\nintro\n"
        "00:00.000 --> 00:02.000 align:start\nHello<br>Привет.\n"
    )
    with TestClient(
        create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False),
        base_url="http://localhost:8765",
    ) as client:
        response = client.post("/api/materials", json=payload(vtt, format="vtt"))
        assert response.status_code == 201, response.text
        material = response.json()
        assert material["adapter_version"] == "spacy-3.8.11/en_core_web_sm-3.8.0/1"
        cue = material["cues"][0]
        assert cue["text"] == "Hello Привет."
        uncertain = next(token for token in cue["tokens"] if token["surface"] == "Привет")
        assert uncertain["uncertain"] is True
        assert (uncertain["lemma"], uncertain["part_of_speech"]) == ("привет", "X")
