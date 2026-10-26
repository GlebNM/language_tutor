"""Text reading through public material operations."""

from fastapi.testclient import TestClient

from backend.app.main import create_learner_app
from backend.tests.test_learning import learning_mcp as learning_mcp


def test_text_can_be_added_selected_and_reopened_without_video(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with TestClient(
        create_learner_app(path, seed_demo=False, include_ai=False), base_url="http://localhost"
    ) as client:
        added = client.post(
            "/api/materials/text",
            json={"title": "Evening", "content": "We watched a film.\n\nThen we went home."},
        )
        assert added.status_code == 201, added.text
        material = added.json()
        assert material["kind"] == "text"
        assert material["youtube_video_id"] is None
        assert material["youtube_url"] is None
        assert len(material["blocks"]) == 2
        paragraph = material["blocks"][0]
        assert paragraph["kind"] == "paragraph"
        assert paragraph["start_ms"] is None and paragraph["end_ms"] is None
        watched = next(t for t in paragraph["tokens"] if t["surface"] == "watched")
        selected = client.get(
            f"/api/materials/{material['id']}/selection",
            params={"cue_id": paragraph["id"], "token_id": watched["id"]},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["block"]["id"] == paragraph["id"]
        assert selected.json()["youtube_url"] is None
    with TestClient(
        create_learner_app(path, seed_demo=False, include_ai=False), base_url="http://localhost"
    ) as client:
        assert client.get(f"/api/materials/{material['id']}").json() == material


def test_text_revisions_and_reading_position_preserve_original_context(tmp_path):
    with TestClient(
        create_learner_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False),
        base_url="http://localhost",
    ) as client:
        first = client.post(
            "/api/materials/text", json={"title": "First", "content": "We read.\n\nWe rest."}
        ).json()
        endpoint = f"/api/materials/{first['id']}/reading-position"
        position = {"block_id": first["blocks"][1]["id"], "offset": 3}
        assert client.put(endpoint, json=position).json() == position
        assert client.get(endpoint).json() == position
        changed = client.put(
            f"/api/materials/{first['id']}/text",
            json={"title": "Later", "content": "We read again."},
        )
        assert changed.status_code == 201, changed.text
        assert changed.json()["previous_version_id"] == first["id"]
        assert changed.json()["version"] == 2
        assert client.get(f"/api/materials/{first['id']}").json() == first
        assert (
            client.put(
                endpoint, json={"block_id": changed.json()["blocks"][0]["id"], "offset": 0}
            ).status_code
            == 422
        )
        assert (
            client.put(
                endpoint, json={"block_id": first["blocks"][0]["id"], "offset": 1000}
            ).status_code
            == 422
        )


def test_text_card_and_citation_use_real_paragraph_via_mcp(learning_mcp):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from backend.app.learning import create_learning_router

    mcp_url, database = learning_mcp
    selected = {}

    def answer(messages, info):
        if info.output_tools[0].name == "final_result" and "translation" in str(
            info.output_tools[0].parameters_json_schema
        ):
            assert "Anna borrowed a book." in str(messages)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {
                            "translation": "взяла на время",
                            "explanation": "Книгу нужно вернуть.",
                            "expression": "borrowed",
                            "example": "I borrowed a pen.",
                        },
                    )
                ]
            )
        if not any(
            isinstance(p, ToolReturnPart) and p.tool_name == "get_subtitle_context"
            for m in messages
            for p in m.parts
        ):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "get_subtitle_context",
                        {
                            "material_id": selected["material_id"],
                            "cue_id": selected["cue_id"],
                            "neighbors": 0,
                        },
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "answer": "Anna borrowed a book and will return it.",
                        "citation_ids": [selected["material_id"] + ":" + selected["cue_id"]],
                    },
                )
            ]
        )

    app = create_learner_app(database, seed_demo=False, include_ai=False)
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(answer),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        material = client.post(
            "/api/materials/text",
            json={"title": "Anna", "content": "Anna borrowed a book. She will return it tomorrow."},
        ).json()
        cue = material["blocks"][0]
        token = next(t for t in cue["tokens"] if t["surface"] == "borrowed")
        selected.update(material_id=material["id"], cue_id=cue["id"], token_id=token["id"])
        card = client.post("/api/cards", json=selected)
        assert card.status_code == 200, card.text
        assert card.json()["card"]["translation"] == "взяла на время"
        conversation = client.post("/api/conversations", json=selected).json()
        response = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"message": "Show the example", "request_id": "text-citation"},
        )
        assert response.status_code == 200, response.text
        citation = response.json()["citations"][0]
        assert citation["kind"] == "text"
        assert citation["start_ms"] is None
        assert citation["youtube_url"] is None
        assert citation["material_url"] == f"#material/{material['id']}?block={cue['id']}"


def test_reading_does_not_create_listening_or_playback_evidence(tmp_path):
    with TestClient(
        create_learner_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False),
        base_url="http://localhost",
    ) as client:
        material = client.post(
            "/api/materials/text", json={"title": "Rest", "content": "We read a book."}
        ).json()
        profile = client.get("/api/profile").json()
        assert client.post(f"/api/materials/{material['id']}/viewing", json={}).status_code == 422
        response = client.post(
            "/api/progress/playback",
            json={
                "material_id": material["id"],
                "generation": profile["storage_generation"],
                "intervals": [
                    {
                        "id": "text-playback",
                        "started_at": "2026-10-26T10:00:00Z",
                        "ended_at": "2026-10-26T10:00:02Z",
                    }
                ],
            },
        )
        assert response.status_code == 422
        assert client.get("/api/dictionary").json() == []
