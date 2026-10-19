"""Protected atomic application storage for SIWC credentials and registration metadata."""

import os
import tempfile
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel

from .siwc_credentials import SIWCCredentials


class Registration(BaseModel):
    client_id: str
    subject: str | None = None
    issuer: str = "https://auth.openai.com"


class CredentialStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "subscription.json"
        self.enabled = True

    def write(self, path: Path, contents: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        descriptor, name = tempfile.mkstemp(dir=self.directory, prefix=".subscription-")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def host_id(self) -> str:
        path = self.directory / "host-id"
        if not path.exists():
            self.write(path, f"urn:uuid:{uuid4()}")
        return path.read_text()

    def registration(self) -> Registration | None:
        path = self.directory / "registration.json"
        return Registration.model_validate_json(path.read_text()) if path.exists() else None

    def save_registration(self, registration: Registration) -> None:
        self.write(self.directory / "registration.json", registration.model_dump_json())

    async def load(self) -> SIWCCredentials:
        if not self.enabled:
            raise ValueError("Subscription disconnected")
        return SIWCCredentials.model_validate_json(self.path.read_text())

    async def save(self, credentials: SIWCCredentials) -> None:
        if not self.enabled:
            raise ValueError("Subscription disconnected")
        self.write(self.path, credentials.model_dump_json())
        self.save_registration(
            Registration(
                client_id=credentials.client_id,
                subject=credentials.subject,
                issuer=credentials.issuer,
            )
        )

    def disconnect(self) -> None:
        self.enabled = False
        self.path.unlink(missing_ok=True)

    def forget_registration(self) -> None:
        """Remove only this application's retained subscription identity after data reset."""
        self.disconnect()
        for name in ("registration.json", "host-id"):
            (self.directory / name).unlink(missing_ok=True)
