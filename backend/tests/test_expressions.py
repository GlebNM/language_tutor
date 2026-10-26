"""Expression behavior through learner APIs, validated agents and the real MCP boundary."""

import asyncio
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from backend.app.agents.expressions import ExpressionCandidate
from backend.app.expressions import ExpressionSource, ExpressionStore
from backend.app.main import create_app, create_learner_app
from backend.app.materials import MIGRATIONS, MaterialError
from backend.tests.test_learning import learning_mcp as learning_mcp

TEXTS = [
    "She gave up smoking.",
    "He turned it off.",
    "The plane took off.",
    "She took off her coat.",
    "Please make a decision.",
    "Let's break the ice.",
    "I look forward",
    "to seeing you.",
    "I put the book on the table.",
    "He is giving up sweets.",
]


def imported(client, texts=TEXTS):
    content = "\n\n".join(
        f"{i + 1}\n00:00:{i * 4:02d},000 --> 00:00:{i * 4 + 3:02d},000\n{text}"
        for i, text in enumerate(texts)
    )
    response = client.post(
        "/api/materials",
        json={
            "youtube_url": "https://youtu.be/TLkA0RELQ1g",
            "title": "Expression practice",
            "format": "srt",
            "content": content,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def payload(messages):
    return json.loads(
        next(
            part.content
            for message in reversed(messages)
            for part in message.parts
            if isinstance(part, UserPromptPart)
        )
    )


def controlled_output(data):
    """Authored expected meanings; this model tests transport/anchoring, not AI accuracy."""
    output = []
    for cue in data["cues"]:
        text = cue["text"].lower()
        tokens = cue["tokens"]
        canonical, kind, sense, surfaces = "", "phrasal_verb", "", []
        if "gave up" in text:
            canonical, sense, surfaces = "give up", "stop-doing", ["gave", "up"]
        elif "giving up" in text:
            canonical, sense, surfaces = "give up", "stop-doing", ["giving", "up"]
        elif "turned it off" in text:
            canonical, sense, surfaces = "turn off", "stop-device", ["turned", "off"]
        elif "plane took off" in text:
            canonical, sense, surfaces = "take off", "depart-airborne", ["took", "off"]
        elif "took off her coat" in text:
            canonical, sense, surfaces = "take off", "remove-clothing", ["took", "off"]
        elif "make a decision" in text:
            canonical, kind, sense, surfaces = (
                "make a decision",
                "collocation",
                "decide",
                ["make", "a", "decision"],
            )
        elif "break the ice" in text:
            canonical, kind, sense, surfaces = (
                "break the ice",
                "idiom",
                "ease-social-tension",
                ["break", "the", "ice"],
            )
        elif "look forward" in text:
            canonical, sense, surfaces = (
                "look forward to",
                "anticipate-pleasure",
                ["look", "forward"],
            )
        if not canonical:
            continue
        ids = [
            next(t["id"] for t in tokens if t["text"].lower() == surface) for surface in surfaces
        ]
        if canonical == "look forward to":
            following = next(
                (c for c in data["cues"] if c["text"].lower().startswith("to seeing")), None
            )
            if following:
                ids.append(following["tokens"][0]["id"])
            else:
                continue
        focus = data.get("focus_token_ids")
        if focus and (not set(focus) <= set(ids)):
            continue
        known = next(
            (
                e
                for e in data["existing_senses"]
                if e["canonical"] == canonical and e["sense_key"] == sense
            ),
            None,
        )
        output.append(
            {
                "canonical": canonical,
                "kind": kind,
                "sense_key": sense,
                "translation": {
                    "stop-doing": "бросить занятие",
                    "stop-device": "выключить",
                    "depart-airborne": "взлететь",
                    "remove-clothing": "снять одежду",
                }.get(sense, "значение выражения"),
                "explanation": "Объяснение выражения в исходном контексте.",
                "example": "This is a created learning example.",
                "usage": "Паттерн употребления.",
                "token_ids": ids,
                "existing_id": known["id"] if known else None,
            }
        )
    return {"expressions": output}


def expression_model(calls=None, *, invalid=False):
    def respond(messages, info):
        data = payload(messages)
        if calls is not None:
            calls.append(data)
        output = controlled_output(data)
        if invalid and output["expressions"]:
            output["expressions"][0]["token_ids"][0] = "foreign-token"
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    return FunctionModel(respond)


def learner(tmp_path, model=None):
    app = create_learner_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    if model is not None:
        app.state.expression_analysis.model_factory = lambda: model
    return app


def state(client, material):
    response = client.get("/api/expressions/source", params={"material_id": material["id"]})
    assert response.status_code == 200, response.text
    return response.json()


def analyze(client, material):
    started = client.post("/api/expressions/analyze", json={"material_id": material["id"]})
    assert started.status_code == 202, started.text
    for _ in range(200):
        result = state(client, material)
        if result["job"]["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.01)
    pytest.fail("Expression analysis did not finish")


def test_all_types_separated_and_cross_cue_forms_are_persistent_and_deduplicated(tmp_path):
    calls = []
    database = tmp_path / "learning.sqlite3"
    with TestClient(
        learner(tmp_path, expression_model(calls)), base_url="http://localhost"
    ) as client:
        material = imported(client)
        result = analyze(client, material)
        assert result["job"]["status"] == "completed", result
        entries = result["occurrences"]
        assert {e["kind"] for e in entries} == {"phrasal_verb", "idiom", "collocation"}
        assert len(entries) == 8
        give_up = [e for e in entries if e["canonical"] == "give up"]
        assert len(give_up) == 2 and give_up[0]["id"] == give_up[1]["id"]
        separated = next(e for e in entries if e["canonical"] == "turn off")
        anchors = separated["contexts"][0]["anchors"]
        assert [
            separated["contexts"][0]["text"][a["start_offset"] : a["end_offset"]] for a in anchors
        ] == ["turned", "off"]
        assert [
            t["surface"] for t in material["cues"][1]["tokens"] if t["id"] in separated["token_ids"]
        ] == ["turned", "off"]
        assert len(next(e for e in entries if e["canonical"] == "look forward to")["contexts"]) == 2
        assert all(not e["saved"] and e["knowledge"]["score"] is None for e in entries)
        assert client.get("/api/expressions").json() == []
        count = len(calls)
        assert analyze(client, material)["occurrences"] == entries
        assert len(calls) == count
    with TestClient(
        create_learner_app(database, seed_demo=False, include_ai=False), base_url="http://localhost"
    ) as client:
        restored = state(client, material)
        assert restored["occurrences"] == entries
        selected = client.post(
            "/api/expressions/selection",
            json={"material_id": material["id"], "token_ids": [separated["token_ids"][-1]]},
        )
        assert selected.status_code == 200 and selected.json()["occurrences"] == entries


def test_familiarity_is_per_meaning_independent_from_words_and_logs_real_changes(tmp_path):
    with TestClient(learner(tmp_path, expression_model()), base_url="http://localhost") as client:
        material = imported(client)
        entries = analyze(client, material)["occurrences"]
        senses = [e for e in entries if e["canonical"] == "take off"]
        assert len({e["id"] for e in senses}) == 2
        baseline = client.get("/api/dictionary").json()
        endpoint = f"/api/expressions/{senses[0]['id']}/knowledge"
        changed = client.put(endpoint, json={"score": 3}).json()
        assert changed["saved"] and changed["knowledge"]["score"] == 3
        assert len(changed["history"]) == 1
        assert len(client.put(endpoint, json={"score": 3}).json()["history"]) == 1
        assert (
            client.get(f"/api/expressions/{senses[1]['id']}").json()["knowledge"]["score"] is None
        )
        assert client.get("/api/dictionary").json() == baseline
        reset = client.put(endpoint, json={"score": None, "ignored": True}).json()
        assert reset["knowledge"]["score"] is None and reset["knowledge"]["ignored"]
        assert len(reset["history"]) == 2
        assert len(client.get("/api/expressions?score=unassessed&kind=phrasal_verb").json()) == 1
        assert client.get("/api/expressions?search=not-here").json() == []


def test_click_particle_resolves_before_full_scan_and_manual_selection_can_restore_dismissal(
    tmp_path,
):
    calls = []
    with TestClient(
        learner(tmp_path, expression_model(calls)), base_url="http://localhost"
    ) as client:
        material = imported(client, ["He turned it off."])
        tokens = material["cues"][0]["tokens"]
        ids = [t["id"] for t in tokens if t["surface"] in {"turned", "off"}]
        endpoint = "/api/expressions/selection"
        selected = client.post(
            endpoint, json={"material_id": material["id"], "token_ids": [ids[-1]]}
        )
        assert selected.status_code == 200, selected.text
        entry = selected.json()["occurrences"][0]
        assert entry["token_ids"] == ids and entry["kind"] == "phrasal_verb"
        client.post(endpoint, json={"material_id": material["id"], "token_ids": [ids[0]]})
        assert len(calls) == 1
        assert (
            client.post(
                f"/api/expressions/occurrences/{entry['occurrence_id']}/dismiss", json={}
            ).status_code
            == 200
        )
        assert state(client, material)["occurrences"] == []
        analyze(client, material)
        assert state(client, material)["occurrences"] == []
        manual = client.post(
            endpoint, json={"material_id": material["id"], "token_ids": ids, "manual": True}
        )
        assert manual.status_code == 200, manual.text
        assert manual.json()["occurrences"][0]["origin"] == "manual"


def test_invalid_anchors_fail_safely_and_retry_can_finish(tmp_path):
    app = learner(tmp_path, expression_model(invalid=True))
    with TestClient(app, base_url="http://localhost") as client:
        material = imported(client, ["She gave up smoking."])
        failed = analyze(client, material)
        assert failed["job"]["status"] == "failed"
        assert failed["occurrences"] == []
        assert "foreign-token" not in failed["job"]["error"]
        app.state.expression_analysis.model_factory = lambda: expression_model()
        assert analyze(client, material)["job"]["status"] == "completed"
        foreign = client.post(
            "/api/expressions/selection",
            json={"material_id": material["id"], "token_ids": ["foreign-token"]},
        )
        assert foreign.status_code == 422
        assert (
            client.post(
                "/api/expressions/analyze",
                json={"material_id": material["id"], "conversation_id": "other"},
            ).status_code
            == 422
        )


def test_reset_removes_expressions_and_rejects_a_late_result(tmp_path):
    app = learner(tmp_path, expression_model())
    with TestClient(app, base_url="http://localhost") as client:
        material = imported(client, ["She gave up smoking."])
        entry = analyze(client, material)["occurrences"][0]
        client.put(f"/api/expressions/{entry['id']}/knowledge", json={"score": 1})
        library = app.state.materials
        store = ExpressionStore(library)
        source = store.source(ExpressionSource(material_id=material["id"]))
        generation = library.get_profile()["storage_generation"]
        candidate = ExpressionCandidate(
            **controlled_output(
                {
                    "cues": [
                        {
                            "text": c["text"],
                            "tokens": [{"id": t["id"], "text": t["surface"]} for t in c["tokens"]],
                        }
                        for c in source["cues"]
                    ],
                    "existing_senses": [],
                }
            )["expressions"][0]
        )
        assert (
            client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"}).status_code
            == 200
        )
        assert client.get("/api/expressions").json() == []
        with pytest.raises(MaterialError, match="изменились"):
            store.put(source, [candidate], generation=generation)
        assert client.get(f"/api/expressions/{entry['id']}").status_code == 404
        with library.connect() as db:
            for table in (
                "expressions",
                "expression_occurrences",
                "expression_events",
                "expression_jobs",
            ):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_expression_routes_are_isolated_between_accounts(tmp_path):
    from backend.tests.test_accounts import login, register

    app = create_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    with TestClient(app, base_url="http://localhost") as client:
        first = register(client, "Alice")
        material = imported(client, ["She gave up smoking."])
        learner_app = app.state.learners.apps[first["user"]["id"]]
        learner_app.state.expression_analysis.model_factory = lambda: expression_model()
        entry = analyze(client, material)["occurrences"][0]
        client.put(f"/api/expressions/{entry['id']}/knowledge", json={"score": 1})
        register(client, "Bob")
        assert client.get("/api/expressions").json() == []
        assert client.get(f"/api/expressions/{entry['id']}").status_code == 404
        assert (
            client.get(
                "/api/expressions/source", params={"material_id": material["id"]}
            ).status_code
            == 404
        )
        assert (
            client.put(f"/api/expressions/{entry['id']}/knowledge", json={"score": 3}).status_code
            == 404
        )
        login(client, "Alice")
        assert client.get("/api/expressions").json()[0]["knowledge"]["score"] == 1


def test_migration_keeps_prior_word_data_and_backups(tmp_path):
    path = tmp_path / "learning.sqlite3"
    with TestClient(learner(tmp_path), base_url="http://localhost") as client:
        material = imported(client, ["She gave up smoking."])
        before = client.get("/api/materials").json()
    with sqlite3.connect(path) as db:
        for table in (
            "expression_events",
            "expression_jobs",
            "expression_occurrences",
            "expression_knowledge",
            "expressions",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute(f"PRAGMA user_version={len(MIGRATIONS) - 1}")
    with TestClient(learner(tmp_path), base_url="http://localhost") as client:
        assert client.get("/api/materials").json() == before
        assert client.get(f"/api/materials/{material['id']}").status_code == 200
        assert client.get("/api/expressions").json() == []
    assert list(tmp_path.glob("*.bak"))


def test_reset_cancels_background_analysis(tmp_path):
    import threading

    entered = threading.Event()

    async def respond(messages, info):
        entered.set()
        await asyncio.sleep(5)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, controlled_output(payload(messages)))]
        )

    app = learner(tmp_path, FunctionModel(respond))
    with TestClient(app, base_url="http://localhost") as client:
        material = imported(client, ["She gave up smoking."])
        assert (
            client.post(
                "/api/expressions/analyze", json={"material_id": material["id"]}
            ).status_code
            == 202
        )
        assert entered.wait(2)
        assert (
            client.post("/api/data/reset", json={"confirmation": "Удалить мои данные"}).status_code
            == 200
        )
        assert app.state.expression_analysis.tasks == {}
        with app.state.materials.connect() as db:
            assert db.execute("SELECT count(*) FROM expression_jobs").fetchone()[0] == 0


def test_expression_context_and_practice_cross_the_real_mcp_boundary(learning_mcp):
    from backend.app.learning import create_learning_router

    mcp_url, database = learning_mcp
    app = create_learner_app(database, seed_demo=False, include_ai=False)
    app.state.expression_analysis.model_factory = lambda: expression_model()
    saved = {}

    def respond(messages, info):
        from pydantic_ai.messages import ToolReturnPart

        results = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "get_expression_context"
        ]
        if not results:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "get_expression_context",
                        {
                            "expression_id": saved["id"],
                            "conversation_id": "wrong-conversation",
                        },
                    )
                ]
            )
        assert "expressions" in str(messages)
        context = json.loads(results[-1].content)
        assert context["knowledge"]["score"] == 1
        assert context["canonical"] == "give up"
        assert context["history"][0]["current"]["score"] == 1
        assert context["occurrences"][0]["text"] == "She gave up smoking."

        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "answer": "Is there a habit you would like to give up?",
                        "citation_ids": [context["occurrences"][0]["citation_id"]],
                    },
                )
            ]
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
        material = imported(client, ["She gave up smoking."])
        saved.update(analyze(client, material)["occurrences"][0])
        client.put(f"/api/expressions/{saved['id']}/knowledge", json={"score": 1})
        conversation = client.post("/api/conversations", json={}).json()
        sent = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={
                "message": "Let's talk about habits.",
                "request_id": "expression-practice",
            },
        )
        assert sent.status_code == 200, sent.text
        assert any(
            t["name"] == "get_expression_context" and t["success"] for t in sent.json()["tools"]
        )
        assert sent.json()["citations"][0]["text"] == "She gave up smoking."


def test_shared_expression_context_omits_conversation_specific_card_text(tmp_path):
    app = learner(tmp_path, expression_model())
    with TestClient(app, base_url="http://localhost") as client:
        material = imported(client, ["She gave up smoking."])
        identity = analyze(client, material)["occurrences"][0]["id"]
        with app.state.materials.connect() as db:
            db.execute(
                "UPDATE expressions SET explanation=?,example=?,usage=? WHERE id=?",
                ("private-conversation-anchor", "private-example", "private-usage", identity),
            )
        context = ExpressionStore(app.state.materials).context(identity, "another-conversation")
        assert "private-" not in json.dumps(context)
        assert context["occurrences"][0]["text"] == "She gave up smoking."


def test_phrasal_match_cannot_borrow_missing_verb_from_a_saved_definition(tmp_path):
    def respond(messages, info):
        data = payload(messages)
        ids = [t["id"] for t in data["cues"][0]["tokens"][:2]]
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "expressions": [
                            {
                                "canonical": "look forward to",
                                "kind": "phrasal_verb",
                                "sense_key": "anticipate",
                                "translation": "ждать",
                                "explanation": "Неверные якоря без глагола.",
                                "example": "I look forward to it.",
                                "usage": "",
                                "token_ids": ids,
                            }
                        ]
                    },
                )
            ]
        )

    with TestClient(
        learner(tmp_path, FunctionModel(respond)), base_url="http://localhost"
    ) as client:
        material = imported(client, ["to seeing you."])
        result = analyze(client, material)
        assert result["job"]["status"] == "failed"
        assert result["occurrences"] == []


def test_manual_phrase_in_completed_message_is_scoped_and_survives_conversation_deletion(tmp_path):
    from backend.app.expressions import now
    from backend.app.message_words import MessageWords

    app = learner(tmp_path, expression_model())
    library = app.state.materials
    with library.connect() as db:
        for identity in ("first-chat", "second-chat"):
            db.execute("INSERT INTO conversations(id,created_at) VALUES(?,?)", (identity, now()))
        db.execute(
            "INSERT INTO learning_messages(conversation_id,request_id,user_text,"
            "response_json,created_at,status) VALUES(?,?,?,?,?,'complete')",
            (
                "first-chat",
                "reply-one",
                "Describe it.",
                json.dumps({"answer": "He turned it off."}),
                now(),
            ),
        )
    tokens = MessageWords(library).words("first-chat", "reply-one")["tokens"]
    ids = [t["id"] for t in tokens if t["surface"] in {"turned", "off"}]
    with TestClient(app, base_url="http://localhost") as client:
        body = {
            "conversation_id": "first-chat",
            "message_id": "reply-one",
            "token_ids": ids,
            "manual": True,
        }
        result = client.post("/api/expressions/selection", json=body)
        assert result.status_code == 200, result.text
        entry = result.json()["occurrences"][0]
        assert entry["token_ids"] == ids and entry["origin"] == "manual"
        assert (
            client.post(
                "/api/expressions/selection", json={**body, "conversation_id": "second-chat"}
            ).status_code
            == 404
        )
        assert (
            client.put(f"/api/expressions/{entry['id']}/knowledge", json={"score": 1}).status_code
            == 200
        )
        # The conversation API removes messages first; its cascade removes only their examples.
        with library.connect() as db:
            db.execute("DELETE FROM learning_messages WHERE conversation_id='first-chat'")
            db.execute("DELETE FROM conversations WHERE id='first-chat'")
        restored = client.get(f"/api/expressions/{entry['id']}").json()
        assert restored["knowledge"]["score"] == 1 and restored["examples"] == []
