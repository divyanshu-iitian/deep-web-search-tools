"""Deterministic, inspectable professional identity evidence scoring."""
import re
from urllib.parse import urlparse, urlunparse

from deepsearch.models import Evidence, IdentityStatus, SearchHit, SearchRequest


def normalized(value: str | None) -> str:
    return " ".join(re.findall(r"\w+", (value or "").casefold()))


def contains_phrase(text: str, phrase: str | None) -> bool:
    target = normalized(phrase)
    return bool(target and f" {target} " in f" {normalized(text)} ")


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


def evaluate_hit(
    request: SearchRequest, hit: SearchHit,
    company_page: str | None = None, github_profile: dict | None = None,
) -> Evidence:
    host = (urlparse(hit.url).hostname or "").lower()
    official = bool(request.company_domain and
                    (host == request.company_domain or host.endswith("." + request.company_domain)))
    summary = f"{hit.title} {hit.snippet}"
    page = company_page or ""
    github = " ".join(str(github_profile.get(key) or "") for key in ("name", "company", "bio", "email")) if github_profile else ""
    name_in_result = contains_phrase(summary, request.name)
    name_in_page = bool(page and contains_phrase(page, request.name))
    name_in_github = bool(github_profile and contains_phrase(str(github_profile.get("name") or ""), request.name))
    company_match = bool(request.company and
                         (contains_phrase(summary, request.company) or
                          contains_phrase(page, request.company) or
                          contains_phrase(github, request.company)))
    exact_email = bool(request.work_email and
                       (request.work_email in summary.casefold() or
                        request.work_email in page.casefold() or
                        request.work_email == str((github_profile or {}).get("email") or "").casefold()))
    signals = []
    if name_in_result: signals.append("full name in search result")
    if name_in_page: signals.append("full name on company page")
    if name_in_github: signals.append("full name on public GitHub profile")
    if company_match: signals.append("company name matches")
    if official: signals.append("company domain")
    if exact_email: signals.append("exact work email publicly listed")
    score = min(100, 25 * name_in_result + 35 * name_in_page + 30 * name_in_github +
                15 * company_match + 20 * official + 35 * exact_email +
                10 * bool(github_profile))
    if not (name_in_result or name_in_page or name_in_github):
        score = min(score, 20)
    if company_page and official:
        category, excerpt = "company_page", page[:1000]
    elif github_profile:
        category, excerpt = "github_public_profile", github[:1000]
    else:
        category, excerpt = "search_result", hit.snippet[:1000]
    return Evidence(url=canonical_url(hit.url), host=host, title=hit.title,
                    excerpt=excerpt, source=hit.provider, observed_at=hit.observed_at,
                    signals=signals, score=score, category=category)


def resolve_status(evidence: list[Evidence]) -> tuple[IdentityStatus, str]:
    official = [item for item in evidence if item.category == "company_page"
                and "full name on company page" in item.signals]
    independent = [item for item in evidence if item.category == "github_public_profile"
                   and "company name matches" in item.signals
                   and "full name on public GitHub profile" in item.signals]
    if official:
        official_hosts = {item.host for item in official}
        if any(item.host not in official_hosts and item.score >= 55 for item in independent):
            return IdentityStatus.corroborated, "Company page and an independent source support the same professional identity."
        return IdentityStatus.supported, "A page on the stated company domain names this person; human review is still appropriate."
    if independent or any(item.score >= 40 and "company name matches" in item.signals
                          and "full name in search result" in item.signals for item in evidence):
        return IdentityStatus.possible, "Search results or a public profile match name and company; no authoritative company page confirmed it."
    return IdentityStatus.unresolved, "No sufficiently specific public professional match was found."
