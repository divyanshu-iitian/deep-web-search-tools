"""Institution-reviewed social accounts; indexed posts stay attributed to a source."""
import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, field_validator, model_validator

from deepsearch.models import SearchHit


class OfficialAccount(BaseModel):
    domain: str
    platform: str
    handle: str
    account_url: str
    proof_url: str
    proof_note: str

    @field_validator("platform")
    @classmethod
    def supported_platform(cls, value):
        if value not in {"linkedin", "facebook"}:
            raise ValueError("Only URL-attributable post platforms are supported")
        return value

    @model_validator(mode="after")
    def approved_provenance(self):
        host = (urlparse(self.proof_url).hostname or "").lower()
        if urlparse(self.proof_url).scheme != "https" or not (host == self.domain or host.endswith("." + self.domain)):
            raise ValueError("Official account proof must be hosted by the institution")
        expected_host = "linkedin.com" if self.platform == "linkedin" else "facebook.com"
        account = urlparse(self.account_url)
        if (account.scheme != "https" or account.hostname not in {expected_host, "www." + expected_host}
                or self.handle not in account.path):
            raise ValueError("Account URL does not match its declared platform and handle")
        return self


@lru_cache(maxsize=1)
def approved_accounts() -> list[OfficialAccount]:
    path = Path(__file__).with_name("official_accounts.json")
    return [OfficialAccount.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]


def account_from_homepage_link(url: str, domain: str, proof_url: str) -> OfficialAccount | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = parsed.path.strip("/").split("/")
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password or not valid_port:
        return None
    if host in {"linkedin.com", "www.linkedin.com"} and len(parts) == 2 and parts[0] == "company":
        platform, handle = "linkedin", parts[1]
    elif host in {"facebook.com", "www.facebook.com"} and len(parts) == 1:
        platform, handle = "facebook", parts[0]
    else:
        return None
    if (not handle or handle.lower() in {"login", "share", "sharer", "posts", "watch"}
            or not all(ch.isalnum() or ch in "-_." for ch in handle)):
        return None
    return OfficialAccount(domain=domain, platform=platform, handle=handle,
                           account_url=url, proof_url=proof_url,
                           proof_note="Account linked from the institution homepage.")


def official_post_account(hit: SearchHit, company_domain: str | None,
                          extra_accounts: list[OfficialAccount] | None = None) -> OfficialAccount | None:
    if not company_domain:
        return None
    parsed = urlparse(hit.url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.strip("/").lower()
    domain = company_domain.lower().removeprefix("www.")
    for account in approved_accounts() + (extra_accounts or []):
        if domain != account.domain and not domain.endswith("." + account.domain):
            continue
        handle = account.handle.lower()
        if account.platform == "linkedin" and host in {"linkedin.com", "www.linkedin.com"}:
            if path.startswith(f"posts/{handle}_"):
                return account
        if account.platform == "facebook" and host in {"facebook.com", "www.facebook.com"}:
            if path.startswith(f"{handle}/posts/"):
                return account
    return None
