"""Serialize semantic fixture judgments using the context review wire contract."""

from typing import Any


def context_output(payload: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for check in checks:
        key = next(
            (
                key
                for key, occurrence in payload["review_keys"].items()
                if occurrence["block_id"] == check["block_id"]
                and occurrence["token_ids"] == check["token_ids"]
            ),
            "unexpected",
        )
        if key in result:
            key = "duplicate"
        result[key] = {
            "context_status": check["context_status"],
            "explanation": check["explanation"],
            "evidence": [item["quote"] for item in check["evidence"]],
            "repair_instructions": check.get("repair_instructions"),
        }
    return {"context_checks": result}
