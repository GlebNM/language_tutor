from fastapi.testclient import TestClient

from backend.app.ai_connection.settings import AISettings
from backend.app.main import create_learner_app as create_app


def test_reset_requires_confirmation_clears_words_and_survives_restart(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with TestClient(create_app(path), base_url="http://localhost:8765") as client:
        settings_store = client.app.state.connection.settings_store
        settings_store.write(AISettings("gpt-6-sol", "high"))
        material = client.get("/api/materials").json()[0]
        loaded = client.get(f"/api/materials/{material['id']}").json()
        cue = next(cue for cue in loaded["cues"] if cue["tokens"])
        token = cue["tokens"][0]
        assert (
            client.put(
                f"/api/lexemes/{token['lexeme_id']}/knowledge", json={"score": 3}
            ).status_code
            == 200
        )
        assert client.post("/api/data/reset", json={"confirmation": "yes"}).status_code == 422
        assert len(client.get("/api/materials").json()) == 1
        generation = client.get("/api/profile").json()["storage_generation"]
        assert (
            client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"}).status_code
            == 200
        )
        assert settings_store.read() == AISettings("gpt-6-luna", "low")
        assert client.get("/api/materials").json() == []
        assert client.get("/api/dictionary").json() == []
        assert client.get(f"/api/materials/{material['id']}").status_code == 404
        assert (
            client.get(
                f"/api/materials/{material['id']}/selection",
                params={"cue_id": cue["id"], "token_id": token["id"]},
            ).status_code
            == 404
        )
        assert client.get("/api/profile").json()["storage_generation"] != generation
    with TestClient(create_app(path), base_url="http://localhost:8765") as client:
        assert client.app.state.connection.settings_store.read() == AISettings("gpt-6-luna", "low")
        assert client.get("/api/materials").json() == []
        assert client.get("/api/dictionary").json() == []
        example = client.post("/api/materials/demo", json={})
        assert example.status_code == 201
        assert example.json()["id"] != material["id"]
        assert len(client.get("/api/materials").json()) == 1


def test_disconnect_preserves_materials_and_manual_assessments(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with TestClient(create_app(path), base_url="http://localhost:8765") as client:
        settings_store = client.app.state.connection.settings_store
        settings_store.write(AISettings("gpt-6-sol", "high"))
        material = client.get("/api/materials").json()[0]
        loaded = client.get(f"/api/materials/{material['id']}").json()
        token = next(c["tokens"][0] for c in loaded["cues"] if c["tokens"])
        client.put(f"/api/lexemes/{token['lexeme_id']}/knowledge", json={"score": None})
        profile = client.get("/api/profile").json()
        dictionary = client.get("/api/dictionary").json()
        assert client.delete("/api/ai/connection").status_code == 200
        assert settings_store.read() == AISettings("gpt-6-sol", "high")
        assert client.get("/api/profile").json() == profile
        assert client.get("/api/dictionary").json() == dictionary
        assert client.get(f"/api/materials/{material['id']}").status_code == 200
        assert client.post("/api/ai/proof").status_code == 409
