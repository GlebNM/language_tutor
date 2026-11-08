"""Generation's final material save and restart recovery through public operations."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_learner_app
from backend.app.materials import MaterialError


def saving_job(library, identity, status="saving"):
    """Establish the persisted state at the save/crash boundary in the real job schema."""
    profile = library.get_profile()
    with library.connect() as db:
        db.execute(
            "INSERT INTO text_generation_jobs "
            "(id,storage_generation,request_json,snapshot_json,status,model,created_at,updated_at) "
            "VALUES(?,?,?,? ,?,'controlled-model',?,?)",
            (identity, profile["storage_generation"],
             '{"topic":null,"style":null,"length":"short","difficulty":"gentle"}',
             "{}", status, "2026-10-26T10:00:00+00:00", "2026-10-26T10:00:00+00:00"),
        )
    return profile


def test_saved_generation_identity_is_atomic_and_recovered_after_restart(tmp_path):
    database = tmp_path / "learning.sqlite3"
    app = create_learner_app(database, seed_demo=False, include_ai=False)
    library = app.state.materials
    profile = saving_job(library, "saved-before-crash")
    arguments = {
        "title": "Books", "content": "We read a book.", "source": "generated",
        "generation_job_id": "saved-before-crash",
        "expected_generation": profile["storage_generation"],
        "expected_profile_revision": profile["revision"],
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, repeated = list(pool.map(lambda _: library.import_text(**arguments), range(2)))
    assert first["id"] == repeated["id"]
    assert first["generation_job_id"] == "saved-before-crash"
    assert len(library.list_materials()) == 1
    with TestClient(app, base_url="http://localhost") as client:
        word = first["blocks"][0]["tokens"][0]
        response = client.put(
            f"/api/lexemes/{word['lexeme_id']}/knowledge", json={"score": 3}
        )
        assert response.status_code == 200
    assert library.import_text(**arguments)["id"] == first["id"]
    restarted = create_learner_app(database, seed_demo=False, include_ai=False)
    with TestClient(restarted, base_url="http://localhost") as client:
        recovered = client.get("/api/text-generation/jobs/saved-before-crash").json()
        assert recovered["status"] == "completed"
        assert recovered["result_material_id"] == first["id"]


@pytest.mark.parametrize("status", ["queued", "cancelled", "interrupted", "completed", None])
def test_inactive_generation_cannot_commit_a_late_material(tmp_path, status):
    app = create_learner_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    library = app.state.materials
    if status:
        saving_job(library, "late-save", status)
    with pytest.raises(MaterialError, match="завершено или остановлено"):
        library.import_text(
            title="Late text", content="We read books.", source="generated",
            generation_job_id="late-save",
        )
    assert library.list_materials() == []


def test_new_save_rechecks_profile_and_reset_inside_the_final_commit(tmp_path):
    app = create_learner_app(tmp_path / "learning.sqlite3", seed_demo=False, include_ai=False)
    library = app.state.materials
    profile = saving_job(library, "stale-save")
    imported = library.import_text(title="Vocabulary", content="We read books.")
    with TestClient(app, base_url="http://localhost") as client:
        token = imported["blocks"][0]["tokens"][0]
        assert client.put(
            f"/api/lexemes/{token['lexeme_id']}/knowledge", json={"score": 3}
        ).status_code == 200
    arguments = {
        "title": "Stale text", "content": "We read books.", "source": "generated",
        "generation_job_id": "stale-save", "expected_generation": profile["storage_generation"],
        "expected_profile_revision": profile["revision"],
    }
    with pytest.raises(MaterialError, match="Словарь изменился"):
        library.import_text(**arguments)
    assert [material["id"] for material in library.list_materials()] == [imported["id"]]
    library.reset_learning_data()
    with pytest.raises(MaterialError, match="Данные были удалены"):
        library.import_text(**arguments)
    # A missing expected_generation must still be caught by the persisted job's generation.
    arguments["expected_generation"] = None
    arguments["expected_profile_revision"] = None
    with pytest.raises(MaterialError, match="завершено или остановлено"):
        library.import_text(**arguments)
    assert library.list_materials() == []
