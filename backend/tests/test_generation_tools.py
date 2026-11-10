"""The model must see required word fields in the public MCP schema."""

import pytest
from mcp.server.fastmcp import FastMCP

from backend.app.generation_tools import register_generation_tools
from backend.app.materials import MaterialLibrary


@pytest.mark.asyncio
async def test_word_lookup_schema_exposes_required_lemma_and_part_of_speech(tmp_path):
    server = FastMCP("schema-test")
    register_generation_tools(server, lambda: MaterialLibrary(tmp_path / "learning.sqlite3"))
    tool = next(tool for tool in await server.list_tools()
                if tool.name == "get_generation_word_knowledge")
    words = tool.inputSchema["properties"]["words"]
    assert words["type"] == "array"
    assert words["minItems"] == 1 and words["maxItems"] == 30
    query = tool.inputSchema["$defs"][words["items"]["$ref"].split("/")[-1]]
    assert set(query["required"]) == {"lemma", "part_of_speech"}
    assert query["additionalProperties"] is False
