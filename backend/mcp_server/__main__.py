"""Internal read-only MCP feasibility server. Contains only an authored proof fixture."""

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel


class ProofContext(BaseModel):
    material_id: str
    cue_id: str
    token_id: str
    text: str
    selected_word: str
    start_seconds: float


server = FastMCP(
    "Language Tutor subscription proof",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "8001")),
    stateless_http=True,
    json_response=True,
    log_level="WARNING",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["mcp:*", "127.0.0.1:*", "localhost:*"],
    ),
)


@server.tool()
def get_proof_context(material_id: str, cue_id: str, token_id: str) -> ProofContext:
    """Read the selected authored example. All three IDs must refer to the same example."""
    if (material_id, cue_id, token_id) != ("subscription-proof", "proof-cue", "proof-token"):
        raise ValueError("Unknown selection or mismatched material/cue/token IDs")
    return ProofContext(
        material_id=material_id,
        cue_id=cue_id,
        token_id=token_id,
        text="She gave up smoking.",
        selected_word="gave",
        start_seconds=0,
    )


if __name__ == "__main__":
    server.run(transport="streamable-http")
