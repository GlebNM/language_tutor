"""Versioned reviewed diagnosis pool and normalized English frequency ranks."""

from __future__ import annotations

import json
import random
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parents[2] / "data" / "diagnostics"
POOL_VERSION = 1
DIAGNOSTIC_VERSION = 2
MIN_QUESTIONS = 12
MAX_QUESTIONS = 18


@lru_cache(maxsize=1)
def frequency_ranks() -> dict[str, int]:
    lines = (DATA / "frequency-en-v1.tsv").read_text().splitlines()[1:]
    return {lemma: int(rank) for rank, lemma in (line.split("\t") for line in lines)}


def frequency_group(lemma: str, language: str = "en") -> int | None:
    rank = frequency_ranks().get(lemma.casefold()) if language == "en" else None
    return None if rank is None else 1 if rank <= 1000 else 2 if rank <= 3000 else 3


def frequency_words(
    *,
    language: str = "en",
    group: int | None = None,
    min_rank: int = 1,
    max_rank: int = 6000,
    limit: int = 12,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    """Sample the licensed local frequency dictionary, retaining real ranks."""
    if language != "en" or group not in {None, 1, 2, 3}:
        raise ValueError("Неподдерживаемый язык или частотная группа.")
    if not 1 <= min_rank <= max_rank <= 6000 or not 1 <= limit <= 20:
        raise ValueError("Нужны ранги 1–6000 и не более 20 слов за вызов.")
    if len(exclude or []) > 100 or any(len(word) > 100 for word in exclude or []):
        raise ValueError("Можно исключить до 100 слов длиной до 100 символов.")
    excluded = {word.casefold() for word in exclude or []}
    candidates = [
        {"lemma": lemma, "rank": rank, "group": frequency_group(lemma)}
        for lemma, rank in frequency_ranks().items()
        if min_rank <= rank <= max_rank
        and lemma not in excluded
        and (group is None or frequency_group(lemma) == group)
    ]
    return {
        "language": language,
        "source": "wordfreq 3.1.1, normalized English list v1; CC BY-SA 4.0",
        "total": len(candidates),
        "words": random.SystemRandom().sample(candidates, min(limit, len(candidates))),
    }


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
