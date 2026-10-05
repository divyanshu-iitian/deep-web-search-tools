from datetime import datetime, timezone
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SearchRequest(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    company: str | None = Field(default=None, max_length=160)
    company_domain: str | None = Field(default=None, max_length=255)
    work_email: str | None = Field(default=None, max_length=320)
    source_url: str | None = Field(default=None, max_length=2000)
    demo: bool = False

    @field_validator("name", "company", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("company_domain")
    @classmethod
    def domain(cls, value):
        if not value:
            return None
        value = value.lower().strip().removeprefix("www.")
        if "://" in value or "/" in value or "." not in value:
            raise ValueError("Use a bare company domain such as example.com")
        return value

    @field_validator("work_email")
    @classmethod
    def email(cls, value):
        if not value:
            return None
        value = value.strip().lower()
        if value.count("@") != 1 or "." not in value.split("@", 1)[1]:
            raise ValueError("A valid work email is required")
        return value

    @model_validator(mode="after")
    def enough_context(self):
        if len(self.name.split()) < 2:
            raise ValueError("Use the person's full name")
        return self


class SearchHit(BaseModel):
    provider: str
    query: str
    title: str
    url: str
    snippet: str = ""
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("url")
    @classmethod
    def public_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("HTTPS URL required")
        return value


class Evidence(BaseModel):
    url: str
    host: str
    title: str
    excerpt: str
    source: str
    observed_at: datetime
    signals: list[str]
    score: int
    category: str
    authority_url: str | None = None
    authority_domain: str | None = None


class IdentityStatus(StrEnum):
    corroborated = "corroborated"
    supported = "supported"
    possible = "possible"
    unresolved = "unresolved"


class IdentityReport(BaseModel):
    run_id: str
    name: str
    company: str | None
    company_domain: str | None
    work_email: str | None
    status: IdentityStatus
    explanation: str
    evidence: list[Evidence]
    queries: list[str]
    warnings: list[str] = []
    created_at: datetime = Field(default_factory=utc_now)
