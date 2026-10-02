"""Deterministic, inspectable professional identity evidence scoring."""
import re
from urllib.parse import urlparse, urlunparse

from deepsearch.models import Evidence, IdentityStatus, SearchHit, SearchRequest
from deepsearch.official_social import OfficialAccount, official_post_account


def normalized(value: str | None) -> str:
    return " ".join(re.findall(r"\w+", (value or "").casefold()))


def contains_phrase(text: str, phrase: str | None) -> bool:
    target = normalized(phrase)
    return bool(target and f" {target} " in f" {normalized(text)} ")


def name_variants(name: str) -> list[str]:
    """Small, explicit transliteration set; never reduce a full name to two tokens."""
    tokens = [part for part in normalized(name).split() if part not in {"dr", "prof", "professor", "mr", "ms"}]
    if len(tokens) < 2:
        return []
    base = " ".join(tokens)
    variants = [base]
    surname = tokens[-1]
    alternate = {"chaubey": "choubey", "choubey": "chaubey"}.get(surname)
    if alternate:
        variants.append(" ".join([*tokens[:-1], alternate]))
    return variants


def contains_person_name(text: str, name: str) -> bool:
    return any(contains_phrase(text, candidate) for candidate in name_variants(name))


def short_name_linked_by_email(page: str, name: str, company_domain: str | None, url: str) -> bool:
    """Tie a shortened official profile name to a surname in its nearby work email."""
    variants = name_variants(name)
    if not variants or not company_domain or len(variants[0].split()) < 3:
        return False
    tokens = variants[0].split()
    short_name = " ".join(tokens[:-1])
    if tokens[0] not in urlparse(url).path.lower():
        return False
    surnames = {variant.split()[-1] for variant in variants}
    for match in re.finditer(r"[a-z0-9._%+-]+@[a-z0-9.-]+", page.casefold()):
        local, domain = match.group().rsplit("@", 1)
        if domain != company_domain.lower().removeprefix("www."):
            continue
        local_tokens = set(re.findall(r"[a-z]+", local))
        if tokens[0] not in local_tokens or not (surnames & local_tokens):
            continue
        if contains_phrase(page[max(0, match.start() - 350):match.end() + 100], short_name):
            return True
    return False


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


def relevant_excerpt(page: str, name: str, work_email: str | None) -> str:
    """Show the matched person's context instead of the page header."""
    text = re.sub(r"\s+", " ", page).strip()
    match = re.search(re.escape(name), text, flags=re.I)
    if not match:
        return text[:1000]
    start = max(0, match.start() - 40)
    excerpt = text[start:match.end() + 520].strip()
    if work_email and work_email.casefold() not in excerpt.casefold():
        email_match = re.search(re.escape(work_email), text, flags=re.I)
        if email_match:
            excerpt += " ... " + text[max(0, email_match.start() - 55):email_match.end() + 80].strip()
    return excerpt[:1000]


def evaluate_hit(
    request: SearchRequest, hit: SearchHit,
    company_page: str | None = None, github_profile: dict | None = None,
    social_accounts: list[OfficialAccount] | None = None,
) -> Evidence:
    host = (urlparse(hit.url).hostname or "").lower()
    official = bool(request.company_domain and
                    (host == request.company_domain or host.endswith("." + request.company_domain)))
    official_account = official_post_account(hit, request.company_domain, social_accounts)
    summary = f"{hit.title} {hit.snippet}"
    page = company_page or ""
    github = " ".join(str(github_profile.get(key) or "") for key in ("name", "company", "bio", "email")) if github_profile else ""
    name_in_result = contains_person_name(summary, request.name)
    social_context_match = bool(official_account and request.company and name_in_result and
                                contains_phrase(hit.snippet, request.company))
    name_in_page = bool(page and contains_person_name(page, request.name))
    linked_short_name = bool(official and page and not name_in_page and
                             short_name_linked_by_email(page, request.name, request.company_domain, hit.url))
    name_in_github = bool(github_profile and contains_person_name(str(github_profile.get("name") or ""), request.name))
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
    is_document = urlparse(hit.url).path.lower().endswith(".pdf")
    if name_in_page:
        signals.append("full name on official document" if is_document else "full name on company page")
    if linked_short_name:
        signals.append("short name linked to surname by official email")
    if name_in_github: signals.append("full name on public GitHub profile")
    if company_match: signals.append("company name matches")
    if official: signals.append("company domain")
    if social_context_match:
        signals.append("institution-listed social account")
        signals.append("full name in indexed institutional post")
    if exact_email: signals.append("exact work email publicly listed")
    score = min(100, 25 * name_in_result + 35 * name_in_page + 30 * name_in_github +
                15 * company_match + 20 * official + 35 * exact_email +
                10 * bool(github_profile) + 35 * social_context_match + 30 * linked_short_name)
    if not (name_in_result or name_in_page or name_in_github or linked_short_name):
        score = min(score, 20)
    if company_page and official:
        category, excerpt = ("official_document" if is_document else "company_page"), relevant_excerpt(page, request.name, request.work_email)
    elif github_profile:
        category, excerpt = "github_public_profile", github[:1000]
    elif social_context_match:
        category, excerpt = "official_social_post_indexed", hit.snippet[:1000]
    else:
        category, excerpt = "search_result", hit.snippet[:1000]
    return Evidence(url=canonical_url(hit.url), host=host, title=hit.title,
                    excerpt=excerpt, source=hit.provider, observed_at=hit.observed_at,
                    signals=signals, score=score, category=category,
                    authority_url=official_account.proof_url if official_account else None,
                    authority_domain=official_account.domain if official_account else None)


def resolve_status(evidence: list[Evidence], name_only: bool = False) -> tuple[IdentityStatus, str]:
    official = [item for item in evidence if (
        item.category == "company_page" and "full name on company page" in item.signals
    ) or (
        item.category == "official_document" and "full name on official document" in item.signals
    ) or (item.category in {"company_page", "official_document"} and
          "short name linked to surname by official email" in item.signals)]
    independent = [item for item in evidence if item.category == "github_public_profile"
                   and "company name matches" in item.signals
                   and "full name on public GitHub profile" in item.signals]
    institutional_social = [item for item in evidence if item.category == "official_social_post_indexed"
                            and "full name in indexed institutional post" in item.signals
                            and item.authority_url]
    if official:
        official_hosts = {item.host for item in official}
        if any(item.host not in official_hosts and item.score >= 55 for item in independent):
            return IdentityStatus.corroborated, "Company page and an independent source support the same professional identity."
        return IdentityStatus.supported, "A first-party page or document links this name to the organization; human review is still appropriate."
    if institutional_social:
        return IdentityStatus.supported, "An indexed post from an institution-listed social account names this person; the post text remains search-index evidence."
    if independent or any(item.score >= 40 and "company name matches" in item.signals
                          and "full name in search result" in item.signals for item in evidence):
        return IdentityStatus.possible, "Search results or a public profile match name and company; no authoritative company page confirmed it."
    if evidence and name_only:
        return IdentityStatus.possible, "Name-only search found candidates; provide an organization or verified source to disambiguate."
    return IdentityStatus.unresolved, "No sufficiently specific public professional match was found."
