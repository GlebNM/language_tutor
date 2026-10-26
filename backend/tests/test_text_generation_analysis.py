"""Generation lexical load through the learner's public application interface."""

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_learner_app
from backend.app.text_generation_analysis import create_assessment_router


def client_for(path):
    app = create_learner_app(path, seed_demo=False, include_ai=False)
    app.include_router(create_assessment_router(app.state.materials))
    return TestClient(app, base_url="http://localhost")


def mark_vocabulary(client, text, scores):
    imported = client.post(
        "/api/materials",
        json={
            "youtube_url": "https://youtu.be/TLkA0RELQ1g",
            "title": "Vocabulary",
            "format": "srt",
            "content": "1\n00:00:00,000 --> 00:00:05,000\n" + text,
        },
    )
    assert imported.status_code == 201, imported.text
    for token in imported.json()["cues"][0]["tokens"]:
        if token["lemma"] in scores:
            response = client.put(
                f"/api/lexemes/{token['lexeme_id']}/knowledge",
                json={"score": scores[token["lemma"]]},
            )
            assert response.status_code == 200, response.text


def test_assessment_uses_current_familiarity_without_registering_draft_words(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        mark_vocabulary(client, "We read books. We borrow books.", {
            "we": 3, "read": 2, "book": 2, "borrow": 0,
        })
        before = client.get("/api/dictionary").json()
        response = client.post("/api/text-generation/assess", json={
            "settings": {"length": "short", "difficulty": "gentle"},
            "draft": {"title": "Books", "paragraphs": [
                {"text": "We read books. " * 49 + "We borrow books."},
            ]},
        })
        assert response.status_code == 200, response.text
        report = response.json()
        assert report["metrics"]["counted_tokens"] == 151
        assert report["metrics"]["known_tokens"] == 150
        assert report["metrics"]["explicit_unknown_tokens"] == 1
        assert report["status"] == "pass"
        assert report["insufficient_vocabulary"][0]["lemma"] == "borrow"
        assert report["insufficient_vocabulary"][0]["score"] == 0
        assert client.get("/api/dictionary").json() == before


def measured_text(unfamiliar_count, different=False):
    nouns = ["stories", "letters", "novels", "poems", "papers", "notes"]
    sentences = [
        "We read " + (nouns[index % len(nouns)] if different else "puzzles") + "."
        for index in range(unfamiliar_count)
    ]
    sentences += ["We read books."] * (66 - unfamiliar_count)
    return {"title": "Books", "paragraphs": [{"text": " ".join(sentences) + " books."}]}


@pytest.mark.parametrize(("difficulty", "score", "count", "passes"), [
    ("gentle", 0, 2, True), ("gentle", 0, 3, False),
    ("normal", 0, 6, True), ("normal", 0, 7, False),
    ("rich", 0, 10, True), ("rich", 0, 11, False),
    ("gentle", 1, 8, True), ("gentle", 1, 9, False),
    ("normal", 1, 14, True), ("normal", 1, 15, False),
    ("rich", 1, 20, True), ("rich", 1, 21, False),
])
def test_presets_compare_actual_occurrences_without_rounding(
    tmp_path, difficulty, score, count, passes,
):
    with client_for(tmp_path / "learning.sqlite3") as client:
        mark_vocabulary(client, "We read books. We read puzzles.", {
            "we": 3, "read": 2, "book": 2, "puzzle": score,
        })
        result = client.post("/api/text-generation/assess", json={
            "settings": {"length": "short", "difficulty": difficulty},
            "draft": measured_text(count),
        })
        assert result.status_code == 200, result.text
        report = result.json()
        assert report["metrics"]["counted_tokens"] == 200
        assert (report["status"] == "pass") is passes
        issue = report["insufficient_vocabulary"][0]
        assert issue["occurrence_count"] == count
        assert len(issue["occurrences"]) == count
        assert issue["violates_budget"] is (not passes)


def test_unassessed_words_count_without_hidden_unique_limits_and_all_reach_context_list(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        mark_vocabulary(client, "We read books.", {"we": 3, "read": 2, "book": 2})
        before = client.get("/api/dictionary").json()
        result = client.post("/api/text-generation/assess", json={
            "settings": {"length": "short", "difficulty": "normal"},
            "draft": measured_text(6, different=True),
        })
        report = result.json()
        assert report["metrics"]["unassessed_tokens"] == 6
        assert report["metrics"]["new_unique"] == 6
        assert report["status"] == "pass"
        assert len(report["insufficient_vocabulary"]) == 6
        assert all(word["score"] is None for word in report["insufficient_vocabulary"])
        assert client.get("/api/dictionary").json() == before


def test_generation_rejects_removed_controls_and_empty_drafts(tmp_path):
    with client_for(tmp_path / "learning.sqlite3") as client:
        for extra in ({"targets": []}, {"source": "anything"}, {"format": "story"}):
            result = client.post("/api/text-generation/assess", json={
                "settings": {"length": "short", **extra}, "draft": measured_text(0),
            })
            assert result.status_code == 422
        result = client.post("/api/text-generation/assess", json={
            "draft": {"title": "Title", "paragraphs": [{"text": "  "}]},
        })
        assert result.status_code == 422
        assert client.get("/api/text-generation/profile").json()[
            "profile_sufficiency"
        ] == "insufficient"
