"""Serialize semantic fixture judgments using the context review wire contract."""

import json
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
            "evidence": evidence_keys(payload, check["evidence"]),
            "repair_instructions": check.get("repair_instructions"),
        }
    return {"context_checks": result}


def evidence_keys(payload: dict[str, Any], evidence: list[dict[str, Any]]) -> list[str]:
    return [next((key for key, source in payload["evidence_sources"].items()
                  if item["quote"] in source["quote"]), "unknown-source")
            for item in evidence]


def requirements_output(payload: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"checks": {check["criterion_id"]: {
        "status": check["status"], "explanation": check["explanation"],
        "evidence": evidence_keys(payload, check["evidence"]),
        "repair_instructions": check.get("repair_instructions"),
    } for check in checks}}


def prompt_payload(content: str) -> dict[str, Any]:
    return json.JSONDecoder().raw_decode(content)[0]
