"""Protected, atomic application storage implementing Pydantic AI's credential source."""

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from pydantic_ai.providers.openai_codex import (
    OpenAICodexCredentials,
    OpenAICodexCredentialSource,
)


class CredentialStore(OpenAICodexCredentialSource):
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "subscription.json"
        self.enabled = True

    async def load(self) -> OpenAICodexCredentials:
        if not self.enabled:
            raise ValueError("Subscription disconnected")
        data = json.loads(self.path.read_text())
        credentials = OpenAICodexCredentials(**data)
        if not all((credentials.access_token, credentials.refresh_token, credentials.account_id)):
            raise ValueError("Incomplete connection storage")
        return credentials

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        if not self.enabled:
            raise ValueError("Subscription disconnected")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        descriptor, name = tempfile.mkstemp(dir=self.directory, prefix=".subscription-")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(asdict(credentials), output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def disconnect(self) -> None:
        self.enabled = False
        self.path.unlink(missing_ok=True)
