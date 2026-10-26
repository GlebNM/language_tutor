"""Generation lexical load through the learner's public application interface."""

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
