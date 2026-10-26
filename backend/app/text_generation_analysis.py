"""Dictionary snapshots and deterministic lexical assessment for generated reading."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.diagnostic_data import frequency_group, frequency_ranks
from backend.app.knowledge import knowledge_value
from backend.app.language import english_adapter
from backend.app.materials import MaterialLibrary
from backend.app.vocabulary import CALENDAR_NAMES, NAME_ENTITIES, sound_reason, vocabulary_value

POLICY_VERSION = "generation-lexical-v1"
LENGTHS = {"short": (150, 200), "medium": (250, 350), "long": (450, 550)}
LOAD_LIMITS = {"gentle": (4, 1), "normal": (7, 3), "rich": (10, 5)}


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    topic: str | None = Field(default=None, max_length=300)
    style: str | None = Field(default=None, max_length=300)
    length: Literal["short", "medium", "long"] = "medium"
    difficulty: Literal["gentle", "normal", "rich"] = "gentle"

    @model_validator(mode="after")
    def empty_options(self) -> GenerationRequest:
        self.topic = self.topic or None
        self.style = self.style or None
        return self

    def constraints(self) -> dict[str, Any]:
        low, high = LENGTHS[self.length]
        learning, unfamiliar = LOAD_LIMITS[self.difficulty]
        return {
            **self.model_dump(),
            "language": "en",
            "min_words": low,
            "max_words": high,
            "learning_ratio_max": learning / 100,
            "new_or_unassessed_ratio_max": unfamiliar / 100,
            "policy_version": POLICY_VERSION,
            "required_criteria": ["coherence"]
            + (["topic"] if self.topic else [])
            + (["style"] if self.style else []),
        }


class DraftParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=12000)


class DraftText(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    kind: Literal["draft"] = "draft"
    title: str = Field(min_length=1, max_length=200)
    paragraphs: list[DraftParagraph] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def bounded_text(self) -> DraftText:
        text = self.title + "\n" + "\n".join(p.text for p in self.paragraphs)
        if "\x00" in text or len(text) > 25000:
            raise ValueError("Текст должен быть короче 25 000 символов и без нулевых символов.")
        return self


class CannotGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["blocked"] = "blocked"
    reason_code: str = Field(min_length=1, max_length=100)
    explanation: str = Field(min_length=1, max_length=1500)
    conflicting_constraints: list[str] = Field(default_factory=list, max_length=12)


@dataclass(frozen=True)
class VocabularySnapshot:
    snapshot_id: str
    profile_revision: int
    storage_generation: str
    language: str
    words: dict[tuple[str, str], dict[str, Any]]
    prior_groups: frozenset[int]
    expressions: tuple[dict[str, Any], ...] = ()

    @classmethod
    def capture(cls, library: MaterialLibrary) -> VocabularySnapshot:
        with library.connect() as db:
            db.execute("BEGIN")
            profile = db.execute("SELECT * FROM learning_profile WHERE id=1").fetchone()
            generation = db.execute(
                "SELECT value FROM storage_metadata WHERE key='generation'"
            ).fetchone()[0]
            words = {
                (row["lemma"], row["part_of_speech"]): {
                    "lexeme_id": row["id"],
                    **knowledge_value(row["score"], bool(row["ignored"]), row["source"]),
                }
                for row in db.execute(
                    "SELECT l.id,l.lemma,l.part_of_speech,k.score,k.ignored,k.source "
                    "FROM lexemes l JOIN word_knowledge k ON k.lexeme_id=l.id "
                    "WHERE l.language=?", (profile["target_language"],),
                )
            }
            groups = frozenset(
                row[0] for row in db.execute(
                    "SELECT frequency_group FROM diagnostic_rules WHERE language=? AND enabled=1",
                    (profile["target_language"],),
                )
            )
            expressions = tuple(
                dict(row) for row in db.execute(
                    "SELECT e.id,e.canonical,e.kind,e.sense_key,e.translation,e.explanation,"
                    "k.score,k.ignored FROM expressions e JOIN expression_knowledge k "
                    "ON k.expression_id=e.id WHERE e.language=?",
                    (profile["target_language"],),
                )
            )
            revision = profile["revision"]
            language = profile["target_language"]
        fingerprint = json.dumps(
            {"revision": revision, "generation": generation, "language": language,
             "words": [[*key, value] for key, value in sorted(words.items())],
             "prior_groups": sorted(groups), "expressions": expressions},
            ensure_ascii=False, sort_keys=True,
        )
        return cls(hashlib.sha256(fingerprint.encode()).hexdigest(), revision, generation,
                   language, words, groups, expressions)

    def knowledge(self, lemma: str, part_of_speech: str) -> dict[str, Any]:
        explicit = self.words.get((lemma.casefold(), part_of_speech))
        if explicit is not None:
            return dict(explicit)
        if frequency_group(lemma, self.language) in self.prior_groups:
            return knowledge_value(2, source="prior")
        return knowledge_value(None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id, "profile_revision": self.profile_revision,
            "storage_generation": self.storage_generation, "language": self.language,
            "words": [[*key, value] for key, value in self.words.items()],
            "prior_groups": sorted(self.prior_groups), "expressions": list(self.expressions),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> VocabularySnapshot:
        return cls(
            value["snapshot_id"], value["profile_revision"], value["storage_generation"],
            value["language"], {(row[0], row[1]): row[2] for row in value["words"]},
            frozenset(value["prior_groups"]), tuple(value.get("expressions", [])),
        )


def vocabulary_page(
    snapshot: VocabularySnapshot, category: Literal["known", "learning"], *,
    part_of_speech: str | None = None, offset: int = 0, limit: int = 30,
) -> dict[str, Any]:
    if not 0 <= offset <= 100000 or not 1 <= limit <= 80:
        raise ValueError("Некорректная страница словаря.")
    wanted = {2, 3} if category == "known" else {0, 1}
    entries = [
        {"lemma": lemma, "part_of_speech": pos, **value}
        for (lemma, pos), value in snapshot.words.items()
        if not value["ignored"] and value["score"] in wanted
        and (part_of_speech is None or pos == part_of_speech)
        and sound_reason(lemma) is None
    ]
    if category == "known" and snapshot.prior_groups and part_of_speech is None:
        explicit_lemmas = {key[0] for key in snapshot.words}
        for lemma in frequency_ranks():
            if lemma not in explicit_lemmas and frequency_group(lemma) in snapshot.prior_groups:
                entries.append({"lemma": lemma, "part_of_speech": None,
                                **knowledge_value(2, source="prior")})
    ranks = frequency_ranks()
    entries.sort(key=lambda item: (ranks.get(item["lemma"], 100000), item["lemma"],
                                   item["part_of_speech"] or ""))
    page = entries[offset:offset + limit]
    return {"snapshot_id": snapshot.snapshot_id, "items": page,
            "next_offset": offset + limit if offset + limit < len(entries) else None}


def build_writing_profile(snapshot: VocabularySnapshot) -> dict[str, Any]:
    counts = Counter(str(value["score"]) for value in snapshot.words.values()
                     if not value["ignored"])
    has_known = bool(counts["2"] + counts["3"])
    return {
        "snapshot_id": snapshot.snapshot_id, "profile_revision": snapshot.profile_revision,
        "language": snapshot.language,
        "counts_by_score": {"unknown_0": counts["0"], "learning_1": counts["1"],
                            "recognizing_2": counts["2"], "confident_3": counts["3"],
                            "unassessed": counts["None"]},
        "prior_bands": [{"group": group, "enabled": group in snapshot.prior_groups,
                         "provisional": True} for group in (1, 2, 3)],
        "known_examples": vocabulary_page(snapshot, "known", limit=40)["items"],
        "learning_examples": vocabulary_page(snapshot, "learning", limit=40)["items"],
        "expression_examples": [dict(e) for e in snapshot.expressions if not e["ignored"]][:20],
        "profile_sufficiency": "provisional" if snapshot.prior_groups else
                               "sufficient" if has_known else "insufficient",
        "notice": "Предварительные оценки не подтверждают знание каждого слова; CEFR не выводится.",
    }


def assess_draft(
    snapshot: VocabularySnapshot, draft: DraftText, settings: GenerationRequest, *,
    revision: int = 1, draft_id: str | None = None,
) -> dict[str, Any]:
    if revision < 1:
        raise ValueError("Версия черновика должна быть положительной.")
    encoded = json.dumps(draft.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    identity = draft_id or "draft-" + digest[:20]
    texts = [draft.title, *(paragraph.text for paragraph in draft.paragraphs)]
    adapter = english_adapter()
    features = adapter.vocabulary_features("\n".join(texts))
    blocks = []
    insufficient: dict[tuple[str, str], dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    offset = 0
    for index, text in enumerate(texts):
        block_id = f"{identity}:r{revision}:" + ("title" if index == 0 else f"p{index}")
        tokens = []
        for number, token in enumerate(adapter.tokenize(text)):
            knowledge = snapshot.knowledge(token["lemma"], token["part_of_speech"])
            reason = sound_reason(token["surface"])
            pos, entity = features.get(
                (offset + token["start_offset"], offset + token["end_offset"]), ("", "")
            )
            if reason is None and token["part_of_speech"] == pos == "PROPN" and (
                entity in NAME_ENTITIES
                or entity == "DATE" and token["surface"].casefold() in CALENDAR_NAMES
            ):
                reason = "proper_name"
            vocabulary = vocabulary_value(reason, knowledge)
            occurrence = {**token, "id": f"{block_id}:t{number}", "block_id": block_id,
                          "knowledge": knowledge, "vocabulary": vocabulary}
            tokens.append(occurrence)
            counts["word_count"] += 1
            if not vocabulary["eligible"] or knowledge["ignored"]:
                continue
            counts["counted_tokens"] += 1
            if knowledge["provisional"]:
                counts["provisional_tokens"] += 1
            score = knowledge["score"]
            category = "known" if score in (2, 3) else "learning" if score == 1 else \
                       "explicit_unknown" if score == 0 else "unassessed"
            counts[category + "_tokens"] += 1
            if category == "known":
                continue
            key = (token["lemma"], token["part_of_speech"])
            if key not in insufficient:
                insufficient[key] = {
                    "kind": "word", "lemma": key[0], "part_of_speech": key[1],
                    "score": score, "source": knowledge["source"],
                    "provisional": knowledge["provisional"], "category": category,
                    "occurrence_count": 0, "occurrences": [],
                }
            item = insufficient[key]
            item["occurrence_count"] += 1
            item["occurrences"].append({"block_id": block_id,
                                        "token_ids": [occurrence["id"]],
                                        "surface": token["surface"]})
        blocks.append({"id": block_id, "text": text, "tokens": tokens})
        offset += len(text) + 1
    n = counts["counted_tokens"]
    learning = counts["learning_tokens"]
    unfamiliar = counts["explicit_unknown_tokens"] + counts["unassessed_tokens"]
    metrics = {key: counts[key] for key in (
        "word_count", "counted_tokens", "known_tokens", "learning_tokens",
        "explicit_unknown_tokens", "unassessed_tokens", "provisional_tokens",
    )}
    metrics.update(learning_ratio=learning / n if n else None,
                   new_or_unassessed_ratio=unfamiliar / n if n else None,
                   new_unique=sum(item["score"] in (0, None) for item in insufficient.values()))
    low, high = LENGTHS[settings.length]
    l_max, u_max = LOAD_LIMITS[settings.difficulty]
    violations: list[dict[str, Any]] = []
    if not low <= counts["word_count"] <= high:
        violations.append({"criterion_id": "length", "actual": counts["word_count"],
                           "minimum": low, "maximum": high})
    if not n:
        violations.append({"criterion_id": "empty_vocabulary", "actual": 0})
    if n and learning * 100 > n * l_max:
        violations.append({"criterion_id": "learning_ratio", "actual": learning / n,
                           "maximum": l_max / 100})
    if n and unfamiliar * 100 > n * u_max:
        violations.append({"criterion_id": "new_ratio", "actual": unfamiliar / n,
                           "maximum": u_max / 100})
    profile = build_writing_profile(snapshot)
    if profile["profile_sufficiency"] == "insufficient":
        violations.append({"criterion_id": "insufficient_profile",
                           "explanation": "Пройдите диагностику или уточните знакомые слова."})
    for item in insufficient.values():
        criterion = "learning_ratio" if item["score"] == 1 else "new_ratio"
        item["contributes_to"] = [criterion]
        item["violates_budget"] = any(v["criterion_id"] == criterion for v in violations)
    return {
        "draft_id": identity, "revision": revision, "draft_hash": digest,
        "snapshot_id": snapshot.snapshot_id, "profile_revision": snapshot.profile_revision,
        "policy_version": POLICY_VERSION, "settings": settings.model_dump(),
        "constraints": settings.constraints(), "status": "fail" if violations else "pass",
        "metrics": metrics, "violations": violations,
        "insufficient_vocabulary": list(insufficient.values()),
        "profile_sufficiency": profile["profile_sufficiency"],
        "versioned_draft": {"draft_id": identity, "revision": revision,
                            "draft_hash": digest, "snapshot_id": snapshot.snapshot_id,
                            "title": draft.title, "title_block": blocks[0],
                            "paragraphs": blocks[1:]},
        "semantic_checks_pending": ["context_support", "requirements"],
    }


class AssessmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    settings: GenerationRequest = Field(default_factory=GenerationRequest)
    draft: DraftText


def create_assessment_router(library: MaterialLibrary) -> APIRouter:
    router = APIRouter(prefix="/api/text-generation")

    @router.get("/profile")
    def profile() -> dict[str, Any]:
        return build_writing_profile(VocabularySnapshot.capture(library))

    @router.post("/assess")
    def assess(request: AssessmentRequest) -> dict[str, Any]:
        return assess_draft(VocabularySnapshot.capture(library), request.draft, request.settings)

    return router
