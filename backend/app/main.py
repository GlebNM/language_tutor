"""One local origin for the standalone web application and material API."""

import logging
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.app.local_access import LocalAccessMiddleware
from backend.app.materials import MaterialError, MaterialLibrary


class TrackImport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    youtube_url: str = Field(max_length=500)
    title: str = Field(default="", max_length=200)
    format: Literal["srt", "vtt"]
    content: str
    language: Literal["en"] = "en"
    source: Literal["srt", "vtt", "demo", "youtube-authored", "youtube-automatic"] | None = None
    is_complete: bool = True


class DataReset(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirmation: Literal["Удалить мои данные"]


def create_app(
    database_path: Path | None = None,
    *,
    seed_demo: bool = True,
    frontend_dist: Path | None = None,
    include_ai: bool = True,
) -> FastAPI:
    traces = logging.getLogger("backend.app.agents")
    if not traces.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        traces.addHandler(handler)
    traces.setLevel(logging.INFO)
    traces.propagate = False
    app = FastAPI(title="Language Tutor", version="0.1.0")
    origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:8765,http://127.0.0.1:8765").split(",")
    app.add_middleware(
        LocalAccessMiddleware,
        allowed_origins=[origin.strip() for origin in origins if origin.strip()],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])
    path = database_path or Path(os.getenv("DATA_DIR", "data/local")) / "learning.sqlite3"
    new_database = not path.exists()
    library = MaterialLibrary(path)
    app.state.materials = library
    root = Path(__file__).resolve().parents[2]

    def add_demo() -> dict[str, Any]:
        return library.import_track(
            youtube_url="https://www.youtube.com/watch?v=TLkA0RELQ1g",
            title="Elephants Dream",
            content=(root / "data/demo/elephants-dream.en.srt").read_text(encoding="utf-8"),
            format="srt",
            source="demo",
            is_complete=True,
        )

    if seed_demo and new_database:
        add_demo()
    from backend.app.knowledge import create_knowledge_router

    app.include_router(create_knowledge_router(library))
    connection = None
    if include_ai:
        from backend.app.ai_connection import create_ai_router
        from backend.app.ai_connection.service import ConnectionService
        from backend.app.ai_connection.siwc import SIWCOAuthFlow
        from backend.app.learning import create_learning_router

        data_dir = library.database_path.parent
        mcp_url = os.getenv("MCP_URL", "http://mcp:8001/mcp")
        connection = ConnectionService(
            data_dir=data_dir,
            callback_host="0.0.0.0",
            callback_port=int(os.getenv("AI_CALLBACK_PORT", "1456")),
            oauth_timeout=300,
            flow_factory=SIWCOAuthFlow,
            mcp_url=mcp_url,
            model_name=os.getenv("AI_MODEL", "gpt-6-luna"),
        )
        app.state.connection = connection
        app.include_router(
            create_ai_router(
                data_dir=data_dir,
                mcp_url=mcp_url,
                model_name=os.getenv("AI_MODEL", "gpt-6-luna"),
                callback_port=int(os.getenv("AI_CALLBACK_PORT", "1456")),
                service=connection,
            )
        )
        app.include_router(
            create_learning_router(library=library, connection=connection, mcp_url=mcp_url)
        )

    from backend.app.diagnostics import create_diagnostics_router

    app.include_router(
        create_diagnostics_router(
            library, service=connection, mcp_url=os.getenv("MCP_URL", "http://mcp:8001/mcp")
        )
    )

    @app.exception_handler(MaterialError)
    async def invalid_material(request: Request, exc: MaterialError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_material(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/profile")
    def profile() -> dict[str, Any]:
        return library.get_profile()

    @app.get("/api/materials")
    def materials() -> list[dict[str, Any]]:
        return library.list_materials()

    @app.post("/api/materials", status_code=201)
    def import_material(track: TrackImport) -> dict[str, Any]:
        return library.import_track(**track.model_dump())

    @app.post("/api/materials/demo", status_code=201)
    def prepared_example() -> dict[str, Any]:
        return add_demo()

    @app.post("/api/data/reset")
    async def reset_data(confirmation: DataReset) -> dict[str, str]:
        if connection is not None:
            await connection.disconnect()
            connection.store.forget_registration()
            connection.settings_store.reset()
        library.reset_learning_data()
        return {"status": "reset"}

    @app.get("/api/materials/{material_id}")
    def material(material_id: str) -> dict[str, Any]:
        return library.get_material(material_id)

    @app.put("/api/materials/{material_id}/track")
    def replace_material(material_id: str, track: TrackImport) -> dict[str, Any]:
        return library.import_track(**track.model_dump(), replaces=material_id)

    @app.get("/api/materials/{material_id}/selection")
    def selection(material_id: str, cue_id: str, token_id: str) -> dict[str, Any]:
        return library.selection(material_id, cue_id, token_id)

    distribution = frontend_dist or root / "apps/web/dist"
    if distribution.is_dir():
        app.mount("/", StaticFiles(directory=distribution, html=True), name="web")
    return app
