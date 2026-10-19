"""English analysis with an explicit, persisted processing version."""

from __future__ import annotations

import re
from functools import lru_cache
from threading import Lock
from typing import Any

import spacy
from spacy.language import Language


class EnglishAdapter:
    language = "en"
    version = "spacy-3.8.11/en_core_web_sm-3.8.0/1"

    def __init__(self) -> None:
        self._nlp: Language = spacy.load("en_core_web_sm", disable=["ner", "parser"])
        self._lock = Lock()

    def tokenize(self, text: str) -> list[dict[str, Any]]:
        with self._lock:
            document = self._nlp(text)
        result = []
        for token in document:
            if (
                token.is_space
                or token.is_punct
                or not any(character.isalpha() for character in token.text)
            ):
                continue
            uncertain = (
                not token.lemma_
                or token.pos_ in {"", "X", "SYM"}
                or not re.fullmatch(r"[A-Za-z'’-]+", token.text)
            )
            result.append(
                {
                    "surface": token.text,
                    "start_offset": token.idx,
                    "end_offset": token.idx + len(token.text),
                    "lemma": token.text.casefold() if uncertain else token.lemma_.casefold(),
                    "part_of_speech": "X" if uncertain else token.pos_,
                    "uncertain": uncertain,
                }
            )
        return result

    def lemmatize(self, text: str) -> list[dict[str, Any]]:
        return self.tokenize(text)

    def get_frequency_band(self, lemma: str, part_of_speech: str) -> None:
        # A reviewed frequency list is supplied by the diagnosis ticket.
        return None

    def get_diagnostic_items(self) -> list[dict[str, Any]]:
        return []


@lru_cache(maxsize=1)
def english_adapter() -> EnglishAdapter:
    return EnglishAdapter()
