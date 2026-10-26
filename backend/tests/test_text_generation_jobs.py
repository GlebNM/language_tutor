"""Background text generation through public job operations with controlled AI."""

from fastapi.testclient import TestClient

from backend.app.main import create_learner_app
from backend.app.text_generation_jobs import TextGenerationService, create_text_generation_router


def test_generation_settings_are_available_without_connecting_ai(tmp_path):
    app = create_learner_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    service = TextGenerationService(app.state.materials, None, "http://127.0.0.1:1/mcp")
    app.include_router(create_text_generation_router(service))
    with TestClient(app, base_url="http://localhost") as client:
        settings = client.get("/api/text-generation/settings").json()
        assert settings["available"] is False
        assert settings["difficulty_limits"]["gentle"]["new_or_unassessed_ratio_max"] == 0.01
        assert settings["difficulty_limits"]["normal"]["new_or_unassessed_ratio_max"] == 0.03
        assert settings["difficulty_limits"]["rich"]["new_or_unassessed_ratio_max"] == 0.05
        response = client.post("/api/text-generation/jobs", json={})
        assert response.status_code == 409
        assert client.get("/api/text-generation/jobs").json() == []
