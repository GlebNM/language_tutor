"""Authenticated text retrieval through the real HTTP MCP service."""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from backend.app.accounts import AccountInput, AccountStore
from backend.app.materials import MaterialLibrary


@pytest.fixture
def text_mcp(tmp_path):
    accounts = AccountStore(tmp_path)
    learners = []
    for username in ("reader", "other-reader"):
        account = accounts.register(AccountInput(username=username, password="test-password-123"))
        with accounts.connect() as db:
            token = db.execute(
                "SELECT mcp_token FROM accounts WHERE id=?", (account["id"],)
            ).fetchone()[0]
        library = MaterialLibrary(accounts.learner_directory(account["id"]) / "learning.sqlite3")
        learners.append((token, library))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "backend.mcp_server"],
        env={
            **os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(port), "DATA_DIR": str(tmp_path),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(150):
            assert process.poll() is None, "MCP stopped before readiness"
            try:
                httpx.get(url, timeout=0.1)
                break
            except httpx.TransportError:
                time.sleep(0.02)
        else:
            pytest.fail("MCP did not become ready")
        yield url, learners
    finally:
        process.terminate()
        process.wait(timeout=5)


async def call_json(client, name, arguments):
    result = await client.call_tool(name, arguments)
    return json.loads(result.content[0].text)


async def test_search_uses_real_lemma_pos_matches_and_only_own_texts(text_mcp):
    url, ((token, library), (_, other)) = text_mcp
    material = library.import_text(
        title="Books", content="We borrowed books.\n\nI borrow a book, then return it."
    )
    homonym = library.import_text(title="Bookings", content="We book a table.")
    other.import_text(title="Private", content="She borrows a private book.")
    async with Client(url, auth=token) as client:
        result = await call_json(client, "find_texts_with_word", {"word": "borrowed"})
        assert [text["material_id"] for text in result["texts"]] == [material["id"]]
        assert [paragraph["text"] for paragraph in result["texts"][0]["paragraphs"]] == [
            "We borrowed books.", "I borrow a book, then return it."
        ]
        matches = [
            match for paragraph in result["texts"][0]["paragraphs"]
            for match in paragraph["matches"]
        ]
        expected = [
            word["id"] for block in material["blocks"] for word in block["tokens"]
            if word["lemma"] == "borrow"
        ]
        assert [match["token_id"] for match in matches] == expected
        nouns = await call_json(client, "find_texts_with_word", {
            "word": "books", "part_of_speech": "NOUN",
        })
        assert [text["material_id"] for text in nouns["texts"]] == [material["id"]]
        verbs = await call_json(client, "find_texts_with_word", {
            "word": "book", "part_of_speech": "VERB",
        })
        assert [text["material_id"] for text in verbs["texts"]] == [homonym["id"]]
        assert result["complete"] is True
        assert result["next_cursor"] is None


async def test_search_pages_all_matching_texts_and_long_paragraphs_with_real_token_ids(text_mcp):
    url, ((token, library), _) = text_mcp
    materials = [
        library.import_text(title=f"Book {index}", content="We borrowed a book.")
        for index in range(6)
    ]
    long_text = (
        "We borrowed a book. " * 250 + "We read pages. " * 500 + "We borrowed the last book."
    )
    long_material = library.import_text(title="Long text", content=long_text)
    materials.append(long_material)
    fragments = {}
    found_tokens = set()
    cursor = None
    async with Client(url, auth=token) as client:
        for _ in range(100):
            result = await call_json(client, "find_texts_with_word", {
                "word": "borrow", "limit": 2, "cursor": cursor,
            })
            assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 6000
            assert len(result["texts"]) <= 2
            for text in result["texts"]:
                for paragraph in text["paragraphs"]:
                    key = (text["material_id"], paragraph["block_id"])
                    collected = fragments.setdefault(key, "")
                    assert paragraph["start_offset"] == len(collected)
                    fragments[key] += paragraph["text"]
                    assert paragraph["end_offset"] == len(fragments[key])
                    found_tokens.update(match["token_id"] for match in paragraph["matches"])
            cursor = result["next_cursor"]
            assert result["complete"] is (cursor is None)
            if cursor is None:
                break
        else:
            pytest.fail("Search did not finish pagination")
    assert set(fragments) == {
        (material["id"], material["blocks"][0]["id"]) for material in materials
    }
    assert fragments[(long_material["id"], long_material["blocks"][0]["id"])] == long_text
    assert found_tokens == {
        word["id"] for material in materials for block in material["blocks"]
        for word in block["tokens"] if word["lemma"] == "borrow"
    }


async def test_get_text_pages_complete_paragraphs_without_exposing_other_learners(text_mcp):
    url, ((token, library), (other_token, other)) = text_mcp
    content = "The page said, \"Read books.\"\n" * 400 + "The end.\n\nA second paragraph."
    material = library.import_text(title="A complete text", content=content, source="text_file")
    private = other.import_text(title="Private", content="The other learner's text.")
    paragraphs = {}
    cursor = None
    first_cursor = None
    async with Client(url, auth=token) as client:
        for _ in range(100):
            result = await call_json(client, "get_text", {
                "material_id": material["id"], "cursor": cursor,
            })
            assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 6000
            assert result["material_id"] == material["id"]
            assert result["source"] == "text_file"
            for fragment in result["paragraphs"]:
                collected = paragraphs.setdefault(fragment["block_id"], "")
                assert fragment["start_offset"] == len(collected)
                paragraphs[fragment["block_id"]] += fragment["text"]
            cursor = result["next_cursor"]
            first_cursor = first_cursor or cursor
            assert result["complete"] is (cursor is None)
            if cursor is None:
                break
        else:
            pytest.fail("Text did not finish pagination")
        with pytest.raises(ToolError, match="не найден"):
            await client.call_tool("get_text", {"material_id": private["id"]})
    assert list(paragraphs.values()) == [block["text"] for block in material["blocks"]]
    assert first_cursor is not None
    async with Client(url, auth=other_token) as client:
        with pytest.raises(ToolError, match="заново"):
            await client.call_tool("get_text", {
                "material_id": private["id"], "cursor": first_cursor,
            })
