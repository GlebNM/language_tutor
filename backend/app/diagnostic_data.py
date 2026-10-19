"""Versioned reviewed diagnosis pool and normalized English frequency ranks."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parents[2] / "data" / "diagnostics"
POOL_VERSION = 1


@lru_cache(maxsize=1)
def frequency_ranks() -> dict[str, int]:
    lines = (DATA / "frequency-en-v1.tsv").read_text().splitlines()[1:]
    return {lemma: int(rank) for rank, lemma in (line.split("\t") for line in lines)}


def frequency_group(lemma: str, language: str = "en") -> int | None:
    rank = frequency_ranks().get(lemma.casefold()) if language == "en" else None
    return None if rank is None else 1 if rank <= 1000 else 2 if rank <= 3000 else 3


@lru_cache(maxsize=1)
def pool() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = json.loads((DATA / "items-en-v1.json").read_text())["items"]
    for item in items:
        if item["kind"] == "word" and frequency_group(item["lemma"]) != item["group"]:
            raise RuntimeError("Diagnostic word does not belong to declared frequency group")
    return items


def get_items(
    language: str = "en", kind: str = "word", group: int | None = None, limit: int = 4
) -> list[dict[str, Any]]:
    if language != "en" or kind not in {"word", "sentence"} or group not in {None, 1, 2, 3}:
        raise ValueError("Неподдерживаемый язык, вид или группа заданий.")
    if not 1 <= limit <= 4:
        raise ValueError("За один вызов можно получить от одного до четырёх заданий.")
    return [
        dict(item)
        for item in pool()
        if item["kind"] == kind and (group is None or item["group"] == group)
    ][:limit]
