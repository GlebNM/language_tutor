"""Atomically persisted global model and reasoning selection."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from openai.types.shared.reasoning_effort import ReasoningEffort


@dataclass(frozen=True)
class AISettings:
    model: str
    reasoning_effort: ReasoningEffort | None


class AISettingsStore:
    def __init__(self, directory: Path, *, default_model: str = "gpt-6-luna") -> None:
        self.path = directory / "ai-settings.json"
        self.default = AISettings(model=default_model, reasoning_effort="low")

    def read(self) -> AISettings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return AISettings(
                model=str(data["model"]),
                reasoning_effort=(
                    data["reasoning_effort"]
                    if data.get("reasoning_effort") is not None
                    else None
                ),
            )
        except FileNotFoundError:
            return self.default
        except (OSError, ValueError, KeyError, TypeError):
            # Keep a damaged preference file from preventing the learner opening the app.
            return self.default

    def write(self, settings: AISettings) -> AISettings:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".ai-settings-")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    {"model": settings.model, "reasoning_effort": settings.reasoning_effort},
                    output,
                    ensure_ascii=False,
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
        return settings

    def reset(self) -> AISettings:
        """Restore the install default when the learner deletes local data."""
        self.path.unlink(missing_ok=True)
        return self.default
