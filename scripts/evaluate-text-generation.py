"""Run real generation trials in disposable private accounts inside the app container.

Uses an existing subscription's access token, without copying its refresh token.
No ordinary learner data is changed. Temporary accounts, credentials and texts are
removed on exit; the report contains only settings, counts and check outcomes.
Run with PYTHONPATH=/app .venv/bin/python scripts/evaluate-text-generation.py.
"""

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic_ai.messages import ToolCallPart

from backend.app import text_generation_jobs as jobs
from backend.app.accounts import AccountInput, AccountStore
from backend.app.ai_connection.service import ConnectionService
from backend.app.ai_connection.siwc import TokenManager
from backend.app.ai_connection.storage import CredentialStore
from backend.app.knowledge import KnowledgeService
from backend.app.main import create_app
from backend.app.materials import MaterialLibrary
from backend.app.text_generation_analysis import VocabularySnapshot

REAL_EXECUTE = ConnectionService.execute


async def observe_execute(self, *args, **kwargs):
    try:
        return await REAL_EXECUTE(self, *args, **kwargs)
    except Exception as error:
        chain = []
        seen = set()
        pending = [error]
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            chain.append(type(item).__name__)
            if isinstance(item, BaseExceptionGroup):
                pending.extend(item.exceptions)
            if item.__context__:
                pending.append(item.__context__)
            if item.__cause__:
                pending.append(item.__cause__)
        print(json.dumps({"operation_error_chain": chain}), flush=True)
        raise


ConnectionService.execute = observe_execute
WRITER_STATS = {}
REAL_WRITE = jobs.write_text


async def observe_writer(**kwargs):
    history = kwargs["message_history"]
    before = len(history)
    result = await REAL_WRITE(**kwargs)
    stats = WRITER_STATS.setdefault(kwargs["profile"]["snapshot_id"], {
        "checks": 0, "candidates": set(),
    })
    for message in history[before:]:
        for part in message.parts:
            if isinstance(part, ToolCallPart) and part.tool_name == "check_text_difficulty":
                value = part.args_as_dict()
                stats["checks"] += 1
                candidate = {"title": value["title"], "paragraphs": value["paragraphs"]}
                stats["candidates"].add(hashlib.sha256(
                    json.dumps(candidate, sort_keys=True).encode(),
                ).hexdigest())
    if hasattr(result, "paragraphs"):
        candidate = {"title": result.title, "paragraphs": [p.text for p in result.paragraphs]}
        stats["candidates"].add(hashlib.sha256(
            json.dumps(candidate, sort_keys=True).encode(),
        ).hexdigest())
    return result


jobs.write_text = observe_writer


TOPICS = [
    ("A neighbour helps repair a bicycle", "A natural conversational story", "short"),
    ("How a community garden works", "A clear popular science explanation", "medium"),
    ("A small cinema opens again", None, "medium"),
    (None, None, "short"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=12)
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--output", default="/tmp/text-generation-evaluation.json")
    args = parser.parse_args()
    if not 1 <= args.cases <= 24 or not 1 <= args.parallel <= 3:
        parser.error("Use 1–24 cases and 1–3 concurrent trials")
    source = max(Path("/data/users").glob("*/credentials/subscription.json"),
                 key=lambda p: p.stat().st_mtime)
    original = CredentialStore(source.parent)
    asyncio.run(TokenManager(original).access_token())
    credentials = asyncio.run(original.load())
    if credentials is None or credentials.expires_at - time.time() < 900:
        raise RuntimeError("A connected subscription with at least 15 minutes remaining is needed")
    credentials = credentials.model_copy(update={"refresh_token": "disabled-for-evaluation"})
    root = Path(tempfile.mkdtemp(prefix="lt-generation-eval-"))
    root.chmod(0o700)
    process = None
    results = []
    try:
        store = AccountStore(root)
        cases = []
        for index in range(args.offset, args.offset + args.cases):
            topic, style, length = TOPICS[(index // 3) % len(TOPICS)]
            settings = dict(topic=topic, style=style, length=length,
                            difficulty=("gentle", "normal", "rich")[index % 3])
            username = f"evaluation_{index}"
            password = secrets.token_urlsafe(24)
            account = store.register(AccountInput(username=username, password=password))
            learner = store.learner_directory(account["id"])
            asyncio.run(CredentialStore(learner / "credentials").save(credentials))
            config = learner / "ai-settings.json"
            config.write_text(json.dumps({"model": args.model, "reasoning_effort": "medium"}))
            config.chmod(0o600)
            library = MaterialLibrary(learner / "learning.sqlite3")
            seed = library.import_text(title="Borrowing an umbrella", content=(
                "A neighbour borrowed an umbrella. It kept her dry in the rain. "
                "She gave it back that evening because it belonged to him."
            ))
            knowledge = KnowledgeService(library)
            for block in seed["blocks"]:
                for token in block["tokens"]:
                    if token["lemma"] in {"borrow", "umbrella"}:
                        knowledge.set_manual(token["lexeme_id"],
                                             1 if token["lemma"] == "borrow" else 0)
            with library.connect() as db:
                for group in (1, 2, 3):
                    db.execute("INSERT INTO diagnostic_rules VALUES(?,?,?,?)", ("en", group, 1, 1))
                db.execute("UPDATE learning_profile SET diagnostic_version=1,revision=revision+1")
            cases.append((index, username, password, settings, library))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        os.environ.update(DATA_DIR=str(root), MCP_URL=f"http://127.0.0.1:{port}/mcp")
        env = dict(os.environ, MCP_HOST="127.0.0.1", MCP_PORT=str(port))
        with (root / "mcp.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "backend.mcp_server"],
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(200):
            if process.poll() is not None:
                raise RuntimeError("Temporary MCP stopped")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Temporary MCP did not start")

        def trial(case):
            index, username, password, settings, library = case
            result = {"case": index, "settings": settings, "versions": []}
            started = time.monotonic()
            try:
                app = create_app(root / "learning.sqlite3", seed_demo=False, include_ai=True)
                with TestClient(app, base_url="http://localhost") as client:
                    login = client.post("/api/auth/login", json=dict(
                        username=username, password=password,
                    ))
                    login.raise_for_status()
                    client.headers["x-session-key"] = login.json()["session_key"]
                    response = client.post("/api/text-generation/jobs", json=settings)
                    response.raise_for_status()
                    job_id = response.json()["id"]
                    versions = {}
                    while time.monotonic() - started < 660:
                        response = client.get(f"/api/text-generation/jobs/{job_id}")
                        with library.connect() as db:
                            for row in db.execute(
                                "SELECT revision,analysis_json,context_json,requirements_json "
                                "FROM text_generation_drafts WHERE job_id=? ORDER BY revision",
                                (job_id,),
                            ):
                                analysis = json.loads(row["analysis_json"])
                                reports = [json.loads(row[column]) for column in
                                           ("context_json", "requirements_json") if row[column]]
                                versions[row["revision"]] = {
                                    "revision": row["revision"], "metrics": analysis["metrics"],
                                    "numeric_issues": analysis["violations"],
                                    "semantic_issues": [
                                        {key: issue.get(key) for key in
                                         ("criterion_id", "explanation", "repair_instructions")}
                                        for review in reports for issue in review["blocking_issues"]
                                    ],
                                    "context_passed": json.loads(row["context_json"])["passed"]
                                    if row["context_json"] else None,
                                    "requirements_passed":
                                    json.loads(row["requirements_json"])["passed"]
                                    if row["requirements_json"] else None,
                                }
                        result["versions"] = list(versions.values())
                        if response.status_code == 404:
                            event = client.get("/api/text-generation/jobs").json()["error_event"]
                            result.update(status="failed",
                                          message=event["message"] if event else "")
                            break
                        job = response.json()
                        if job["status"] == "completed":
                            result.update(status="completed", revision=job["revision"],
                                          metrics=job["analysis"]["metrics"])
                            break
                        time.sleep(0.5)
                    else:
                        result["status"] = "deadline"
            except Exception as error:
                result.update(status="error", error_type=type(error).__name__)
            stats = WRITER_STATS.get(VocabularySnapshot.capture(library).snapshot_id, {})
            result["preflight_checks"] = stats.get("checks", 0)
            result["distinct_writer_candidates"] = len(stats.get("candidates", set()))
            result["seconds"] = round(time.monotonic() - started, 2)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as executor:
            results = list(executor.map(trial, cases))
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        shutil.rmtree(root)
        report = {
            "mode": "live subscription, real MCP, disposable synthetic profiles",
            "model": args.model, "reasoning_effort": "medium",
            "cases": results,
            "completed": sum(r["status"] == "completed" for r in results),
            "completed_by_second_version": sum(
                r["status"] == "completed" and r["revision"] <= 2 for r in results
            ),
        }
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: v for k, v in report.items() if k != "cases"}), flush=True)


if __name__ == "__main__":
    main()
