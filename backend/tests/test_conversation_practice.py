"""Conversation practice through the public API and real learning MCP boundary."""

from pathlib import Path

from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from backend.app.learning import create_learning_router
from backend.app.main import create_learner_app as create_app
from backend.tests.test_learning import forbid_live_models as forbid_live_models
from backend.tests.test_learning import learning_mcp as learning_mcp


def practice_client(database: Path) -> TestClient:
    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url="http://127.0.0.1:1/mcp",
            model_factory=lambda: TestModel(),
        )
    )
    return TestClient(app, base_url="http://localhost")


def test_new_conversation_without_material_survives_application_restart(tmp_path: Path) -> None:
    database = tmp_path / "learning.sqlite3"
    with practice_client(database) as client:
        created = client.post("/api/conversations", json={})
        assert created.status_code == 201, created.text
        conversation = created.json()
        assert conversation["messages"] == []
        assert conversation["material_id"] is None
    with practice_client(database) as client:
        restored = client.get(f"/api/conversations/{conversation['id']}")
        assert restored.status_code == 200
        assert restored.json()["id"] == conversation["id"]
        assert [item["id"] for item in client.get("/api/conversations").json()] == [
            conversation["id"]
        ]


def test_conversation_manages_its_title_and_material_context(tmp_path: Path) -> None:
    from backend.tests.test_learning import import_selection

    with practice_client(tmp_path / "learning.sqlite3") as client:
        first = import_selection(client)
        second = import_selection(client, text="They watched the river bank.")
        created = client.post("/api/conversations", json={"title": "Travel plans"})
        assert created.status_code == 201, created.text
        identity = created.json()["id"]
        endpoint = f"/api/conversations/{identity}"
        updated = client.patch(
            endpoint,
            json={
                "title": "A trip by the river",
                "material_ids": [first["material_id"], second["material_id"]],
            },
        )
        assert updated.status_code == 200, updated.text
        restored = client.get(endpoint).json()
        assert restored["title"] == "A trip by the river"
        assert set(restored["material_ids"]) == {first["material_id"], second["material_id"]}
        invalid = client.patch(endpoint, json={"material_ids": ["missing"]})
        assert invalid.status_code == 404
        assert client.get(endpoint).json()["material_ids"] == restored["material_ids"]
        assert client.delete(endpoint).status_code == 200
        assert client.get(endpoint).status_code == 404
        assert client.get("/api/conversations").json() == []
        assert len(client.get("/api/materials").json()) == 2


def test_free_conversations_keep_their_own_messages_and_deduplicate_retries(
    learning_mcp: tuple[str, Path],
) -> None:
    from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    mcp_url, database = learning_mcp
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(
            isinstance(part, ToolReturnPart) for message in messages for part in message.parts
        ):
            return ModelResponse(parts=[ToolCallPart("get_learning_profile", {"language": "en"})])
        seen.append(str(messages))
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "answer": "What do you enjoy about travelling?",
                        "citation_ids": [],
                    },
                )
            ]
        )

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        first = client.post("/api/conversations", json={}).json()["id"]
        second = client.post("/api/conversations", json={}).json()["id"]
        endpoint = f"/api/conversations/{first}/messages"
        incoming = {"message": "My private-anchor is a river trip.", "request_id": "first-message"}
        sent = client.post(endpoint, json=incoming)
        assert sent.status_code == 200, sent.text
        assert client.post(endpoint, json=incoming).json() == sent.json()
        assert len(client.get(f"/api/conversations/{first}").json()["messages"]) == 1
        assert (
            client.post(
                f"/api/conversations/{second}/messages",
                json={
                    "message": "Let's talk about food.",
                    "request_id": "second-message",
                },
            ).status_code
            == 200
        )
        assert "private-anchor" not in seen[-1]
        assert (
            client.post(
                endpoint,
                json={
                    "message": "Continue our topic.",
                    "request_id": "continue-first",
                },
            ).status_code
            == 200
        )
        assert "private-anchor" in seen[-1]
        events = client.get("/api/activity?event_type=message_sent").json()["events"]
        assert len(events) == 3
        assert all(event["lexeme_id"] is None for event in events)


def test_long_conversation_preserves_early_context_and_original_messages(
    learning_mcp: tuple[str, Path],
) -> None:
    from pydantic_ai.messages import (
        ModelMessage,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
    )
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    mcp_url, database = learning_mcp

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        text = str(messages)
        if not info.output_tools:
            return ModelResponse(
                parts=[
                    TextPart(
                        "The learner's favourite plant is baobab."
                        if "baobab" in text
                        else "No plant known."
                    )
                ]
            )
        if not any(
            isinstance(part, ToolReturnPart) for message in messages for part in message.parts
        ):
            return ModelResponse(parts=[ToolCallPart("get_learning_profile", {"language": "en"})])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "answer": "Your favourite plant is baobab."
                        if "What was my favourite plant?" in text and "baobab" in text
                        else "Let us explore another topic.",
                    },
                )
            ]
        )

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        identity = client.post("/api/conversations", json={}).json()["id"]
        endpoint = f"/api/conversations/{identity}"
        for number in range(11):
            message = "My favourite plant is baobab." if number == 0 else f"A new topic {number}."
            # Later replies must not carry the first fact incidentally.
            if number == 10:
                message = "What was my favourite plant?"
            response = client.post(
                endpoint + "/messages",
                json={
                    "message": message,
                    "request_id": f"long-message-{number}",
                },
            )
            assert response.status_code == 200, response.text
        assert response.json()["answer"] == "Your favourite plant is baobab."
        original = client.get(endpoint).json()["messages"]
        assert len(original) == 11
        assert original[0]["message"] == "My favourite plant is baobab."


def test_agent_reads_current_familiarity_transitions_through_mcp(
    learning_mcp: tuple[str, Path],
) -> None:
    import json

    from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from backend.tests.test_learning import import_selection

    mcp_url, database = learning_mcp
    captured: list[dict] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "get_practice_context"
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("get_practice_context", {"language": "en"})])
        captured.append(json.loads(returns[-1].content))
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "answer": "Would you give up a habit to try something new?",
                    },
                )
            ]
        )

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        source = import_selection(client)
        token = client.get(f"/api/materials/{source['material_id']}").json()["cues"][0]["tokens"][1]
        knowledge = f"/api/lexemes/{token['lexeme_id']}/knowledge"
        assert client.put(knowledge, json={"score": 0}).status_code == 200
        assert client.put(knowledge, json={"score": 1}).status_code == 200
        identity = client.post("/api/conversations", json={}).json()["id"]
        sent = client.post(
            f"/api/conversations/{identity}/messages",
            json={
                "message": "Let's talk about habits.",
                "request_id": "practice-history",
            },
        )
        assert sent.status_code == 200, sent.text
        word = next(w for w in captured[-1]["words"] if w["lexeme_id"] == token["lexeme_id"])
        assert word["knowledge"]["score"] == 1
        assert word["history"][0]["current"]["score"] == 1
        assert word["history"][0]["previous"]["score"] == 0
        assert word["history"][0]["occurred_at"]
        client.put(knowledge, json={"score": 1, "ignored": True})
        sent = client.post(
            f"/api/conversations/{identity}/messages",
            json={
                "message": "Continue.",
                "request_id": "practice-ignored",
            },
        )
        assert sent.status_code == 200, sent.text
        assert token["lexeme_id"] not in [w["lexeme_id"] for w in captured[-1]["words"]]


def test_topics_are_suggested_without_a_selected_word(learning_mcp: tuple[str, Path]) -> None:
    from pydantic_ai.models.test import TestModel

    mcp_url, database = learning_mcp
    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: TestModel(
                call_tools=[],
                custom_output_args={
                    "answer": "Choose a topic, or suggest your own.",
                    "suggested_topics": [
                        "A memorable trip",
                        "Everyday habits",
                        "Something worth learning",
                    ],
                },
            ),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        identity = client.post("/api/conversations", json={}).json()["id"]
        result = client.post(f"/api/conversations/{identity}/topics", json={})
        assert result.status_code == 200, result.text
        assert result.json()["suggested_topics"] == [
            "A memorable trip",
            "Everyday habits",
            "Something worth learning",
        ]


def test_stream_delivers_reply_text_and_one_verified_final_response(
    learning_mcp: tuple[str, Path],
) -> None:
    import json

    mcp_url, database = learning_mcp
    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: TestModel(
                call_tools=[],
                custom_output_args={
                    "answer": "What would make your next trip special?",
                },
            ),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        identity = client.post("/api/conversations", json={}).json()["id"]
        result = client.post(
            f"/api/conversations/{identity}/messages/stream",
            json={
                "message": "Let's plan a trip.",
                "request_id": "stream-first",
            },
        )
        assert result.status_code == 200, result.text
        events = [
            json.loads(line.removeprefix("data: "))
            for line in result.text.splitlines()
            if line.startswith("data: ")
        ]
        texts = [event["text"] for event in events if event["type"] == "text"]
        assert texts and texts[-1] == "What would make your next trip special?"
        final = [event["response"] for event in events if event["type"] == "complete"]
        assert len(final) == 1
        assert final[0]["answer"] == texts[-1]
        saved = client.get(f"/api/conversations/{identity}").json()["messages"]
        assert len(saved) == 1 and saved[0]["response"]["answer"] == texts[-1]


def test_stopping_generation_preserves_one_retryable_message(
    learning_mcp: tuple[str, Path],
) -> None:
    import asyncio
    import time
    from concurrent.futures import ThreadPoolExecutor

    from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    mcp_url, database = learning_mcp
    slow = True

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if slow:
            await asyncio.sleep(5)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": "Ready."})])

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client, ThreadPoolExecutor() as pool:
        identity = client.post("/api/conversations", json={}).json()["id"]
        endpoint = f"/api/conversations/{identity}"
        incoming = {"message": "Let's discuss travel.", "request_id": "cancelled-message"}
        from contextvars import copy_context
        pending = pool.submit(
            copy_context().run, client.post, endpoint + "/messages", json=incoming
        )
        deadline = time.monotonic() + 3
        while not client.get(endpoint).json()["messages"] and time.monotonic() < deadline:
            time.sleep(0.01)
        stopped = client.post(endpoint + "/messages/cancelled-message/cancel", json={})
        assert stopped.status_code == 200, stopped.text
        assert pending.result(timeout=3).status_code == 409
        saved = client.get(endpoint).json()["messages"]
        assert len(saved) == 1 and saved[0]["status"] == "cancelled"
        slow = False
        assert client.post(endpoint + "/messages", json=incoming).status_code == 200
        saved = client.get(endpoint).json()["messages"]
        assert len(saved) == 1 and saved[0]["status"] == "complete"


def test_word_panel_uses_message_context_and_shares_dictionary_with_video(
    learning_mcp: tuple[str, Path],
) -> None:
    from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from backend.tests.test_learning import import_selection

    mcp_url, database = learning_mcp

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        properties = info.output_tools[0].parameters_json_schema.get("properties", {})
        if "translation" in properties:
            translation = "берег" if "river bank" in str(messages) else "банк"
            value = {
                "translation": translation,
                "explanation": "Значение в этой реплике.",
                "expression": "bank",
                "example": "We walked along the bank.",
            }
        else:
            value = {"answer": "We rested on the river bank."}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, value)])

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        source = import_selection(client, text="We stood on the river bank.")
        identity = client.post("/api/conversations", json={}).json()["id"]
        other = client.post("/api/conversations", json={}).json()["id"]
        endpoint = f"/api/conversations/{identity}/messages/bank-context"
        sent = client.post(
            f"/api/conversations/{identity}/messages",
            json={
                "message": "Tell me about a river.",
                "request_id": "bank-context",
            },
        )
        assert sent.status_code == 200, sent.text
        words = client.get(endpoint + "/words")
        assert words.status_code == 200, words.text
        bank = next(word for word in words.json()["tokens"] if word["surface"] == "bank")
        selected = client.post(endpoint + f"/words/{bank['id']}/selection", json={})
        assert selected.status_code == 200, selected.text
        selection = {
            "source": "message",
            "conversation_id": identity,
            "message_id": "bank-context",
            "token_id": bank["id"],
        }
        card = client.post("/api/cards", json=selection)
        assert card.status_code == 200, card.text
        assert card.json()["card"]["translation"] == "берег"
        assert card.json()["selection"]["text"] == "We rested on the river bank."
        wrong = client.post("/api/cards", json={**selection, "conversation_id": other})
        assert wrong.status_code == 404
        assert (
            client.put(f"/api/lexemes/{bank['lexeme_id']}/knowledge", json={"score": 1}).status_code
            == 200
        )
        video = client.get(f"/api/materials/{source['material_id']}").json()
        video_bank = next(word for word in video["cues"][0]["tokens"] if word["surface"] == "bank")
        assert video_bank["lexeme_id"] == bank["lexeme_id"]
        assert video_bank["knowledge"]["score"] == 1
        events = client.get("/api/activity?event_type=translation_received").json()["events"]
        assert events[0]["conversation_id"] == identity
        assert events[0]["context"]["text"] == "We rested on the river bank."
        assert client.delete(f"/api/conversations/{identity}").status_code == 200
        assert client.get(endpoint + "/words").status_code == 404
        assert client.get("/api/activity?event_type=translation_received").json()["events"] == []
        assert (
            next(
                w
                for w in client.get("/api/dictionary").json()
                if w["lexeme_id"] == bank["lexeme_id"]
            )["knowledge"]["score"]
            == 1
        )


def test_message_word_can_correct_its_lexeme_without_changing_other_occurrences(
    learning_mcp: tuple[str, Path],
) -> None:
    mcp_url, database = learning_mcp
    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: TestModel(
                call_tools=[], custom_output_args={"answer": "We watched birds."}
            ),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        identity = client.post("/api/conversations", json={}).json()["id"]
        endpoint = f"/api/conversations/{identity}/messages/correct-lemma"
        assert (
            client.post(
                f"/api/conversations/{identity}/messages",
                json={
                    "message": "Tell me a story.",
                    "request_id": "correct-lemma",
                },
            ).status_code
            == 200
        )
        words = client.get(endpoint + "/words").json()["tokens"]
        watched = next(word for word in words if word["surface"] == "watched")
        corrected = client.put(
            endpoint + f"/words/{watched['id']}/lexeme",
            json={
                "lemma": "observe",
                "part_of_speech": "VERB",
            },
        )
        assert corrected.status_code == 200, corrected.text
        fresh = client.get(endpoint + "/words").json()["tokens"]
        assert next(word for word in fresh if word["id"] == watched["id"])["lemma"] == "observe"
        assert next(word for word in fresh if word["surface"] == "birds")["lemma"] == "bird"
        assert len(client.get("/api/activity?event_type=lexeme_corrected").json()["events"]) == 1


def test_activity_tool_cannot_transfer_another_conversation_text(
    learning_mcp: tuple[str, Path],
) -> None:
    from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    mcp_url, database = learning_mcp
    first_id = ""
    captured: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if "INSPECT_HISTORY" in str(messages):
            returned = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_name == "get_recent_activity"
            ]
            if not returned:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "get_recent_activity",
                            {
                                "language": "en",
                                "event_type": "word_selected",
                                "conversation_id": first_id,
                            },
                        )
                    ]
                )
            captured.append(str(messages))
            answer = "Let us choose a new topic."
        else:
            answer = "We considered a hidden red canoe."
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": answer})])

    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        first_id = client.post("/api/conversations", json={}).json()["id"]
        second_id = client.post("/api/conversations", json={}).json()["id"]
        first = f"/api/conversations/{first_id}/messages/source-message"
        assert (
            client.post(
                f"/api/conversations/{first_id}/messages",
                json={
                    "message": "A story please.",
                    "request_id": "source-message",
                },
            ).status_code
            == 200
        )
        token = client.get(first + "/words").json()["tokens"][0]
        assert client.post(first + f"/words/{token['id']}/selection", json={}).status_code == 200
        # The learner sees the original context; other conversations receive only word metadata.
        assert "hidden red canoe" in str(client.get("/api/activity").json())
        result = client.post(
            f"/api/conversations/{second_id}/messages",
            json={
                "message": "INSPECT_HISTORY",
                "request_id": "inspect-message",
            },
        )
        assert result.status_code == 200, result.text
        assert captured and "hidden red canoe" not in captured[-1]


def test_partial_structured_reply_is_visible_before_its_final_json_string_closes(
    learning_mcp: tuple[str, Path],
) -> None:
    import json

    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    async def chunks(messages, info):
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args='{"answer":"Hello')}
        yield {0: DeltaToolCall(json_args=" there")}
        yield {0: DeltaToolCall(json_args='."}')}

    mcp_url, database = learning_mcp
    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(stream_function=chunks),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        identity = client.post("/api/conversations", json={}).json()["id"]
        response = client.post(
            f"/api/conversations/{identity}/messages/stream",
            json={
                "message": "Hello",
                "request_id": "partial-stream",
            },
        )
        assert response.status_code == 200, response.text
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        text = [event["text"] for event in events if event["type"] == "text"]
        assert "Hello" in text
        assert "Hello there" in text
        assert text[-1] == "Hello there."
        assert events[-1]["type"] == "complete"


def test_same_word_in_two_messages_keeps_separate_context_cards(
    learning_mcp: tuple[str, Path],
) -> None:
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    replies = iter(["The bank approved a loan.", "We sat on the river bank."])

    def respond(messages, info):
        if "translation" in info.output_tools[0].parameters_json_schema.get("properties", {}):
            value = {
                "translation": "берег" if "river bank" in str(messages) else "банк",
                "explanation": "Значение в выбранном сообщении.",
                "expression": "bank",
                "example": "We walked along the bank.",
            }
        else:
            value = {"answer": next(replies)}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, value)])

    mcp_url, database = learning_mcp
    app = create_app(
        database_path=database,
        seed_demo=False,
        include_ai=False,
        frontend_dist=database.parent / "no-frontend",
    )
    app.include_router(
        create_learning_router(
            library=app.state.materials,
            connection=None,
            mcp_url=mcp_url,
            model_factory=lambda: FunctionModel(respond),
        )
    )
    with TestClient(app, base_url="http://localhost") as client:
        identity = client.post("/api/conversations", json={}).json()["id"]
        selections = []
        for number in range(2):
            message_id = f"bank-meaning-{number}"
            sent = client.post(
                f"/api/conversations/{identity}/messages",
                json={
                    "message": "Give me another example.",
                    "request_id": message_id,
                },
            )
            assert sent.status_code == 200, sent.text
            words = client.get(f"/api/conversations/{identity}/messages/{message_id}/words").json()
            bank = next(token for token in words["tokens"] if token["surface"] == "bank")
            selections.append(
                {
                    "source": "message",
                    "conversation_id": identity,
                    "message_id": message_id,
                    "token_id": bank["id"],
                }
            )
        first = client.post("/api/cards", json=selections[0])
        second = client.post("/api/cards", json=selections[1])
        assert first.status_code == second.status_code == 200
        assert first.json()["card"]["translation"] == "банк"
        assert second.json()["card"]["translation"] == "берег"
        cached = client.post("/api/cards", json=selections[0]).json()
        assert cached["cached"] is True and cached["card"]["translation"] == "банк"
