from pydantic import BaseModel, Field


class SIWCCredentials(BaseModel):
    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)
    id_token: str = Field(min_length=1, repr=False)
    client_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    issuer: str = "https://auth.openai.com"
    scopes: list[str]
    expires_at: float
