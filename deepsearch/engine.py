"""Bounded search → authoritative-page checks → evidence report."""
import asyncio
import hashlib
import time
from uuid import uuid4

from deepsearch.config import Settings
from deepsearch.company_discovery import discover_company_pages
from deepsearch.identity import canonical_url, evaluate_hit, name_variants, resolve_status
from deepsearch.models import IdentityReport, SearchHit, SearchRequest
from deepsearch.official_social import OfficialAccount
from deepsearch.providers import BraveSearchProvider, DemoProvider, SearchProvider, SearxngProvider
from deepsearch.sources import company_document_text, company_page_text, github_public_profile, github_username, is_company_url
from deepsearch.storage import Storage


def query_plan(request: SearchRequest) -> list[str]:
    variants = name_variants(request.name)
    name = f'"{variants[0]}"'
    if not (request.company or request.company_domain or request.work_email):
        return [f'"{variant}"' for variant in variants][:3]
    if request.company_domain and request.work_email and len(variants) > 1:
        return list(dict.fromkeys([
            f'{name} "{request.company}"' if request.company else f"{name} site:{request.company_domain}",
            f'"{variants[1]}" site:{request.company_domain}',
            f'"{request.work_email}"',
        ]))[:3]
    queries = []
    if request.company:
        queries.append(f'{name} "{request.company}"')
    if request.company_domain:
        queries.append(f"{name} site:{request.company_domain}")
    if request.work_email:
        queries.append(f'"{request.work_email}"')
    if request.company_domain:
        if len(variants) > 1:
            queries.append(f'"{variants[1]}" site:{request.company_domain}')
    elif len(variants) > 1:
        queries.append(f'"{variants[1]}"' + (f' "{request.company}"' if request.company else ''))
    if not queries:
        queries.append(name)
        if len(variants) > 1:
            queries.append(f'"{variants[1]}"')
    return list(dict.fromkeys(queries))[:3]


def company_candidate_rank(hit: SearchHit, name: str) -> int:
    path = hit.url.lower().split("?", 1)[0]
    tokens = name_variants(name)[0].split()
    score = 0
    if tokens[0] in path:
        score += 4
    if len(tokens) > 1 and tokens[1] in path:
        score += 3
    if any(variant.split()[-1] in path for variant in name_variants(name)):
        score += 4
    if any(hint in path for hint in ("faculty", "profile", "staff", "resume", "_cv", "-cv")):
        score += 3
    if path.endswith(".pdf"):
        score += 1
    return score


class SearchEngine:
    def __init__(self, settings: Settings, storage: Storage, provider: SearchProvider | None = None,
                 company_fetcher=company_page_text, github_fetcher=github_public_profile,
                 company_discoverer=discover_company_pages, document_fetcher=company_document_text):
        self.settings = settings
        self.storage = storage
        self.provider = provider
        self.company_fetcher = company_fetcher
        self.document_fetcher = document_fetcher
        self.github_fetcher = github_fetcher
        self.company_discoverer = company_discoverer
        self._rate_lock = asyncio.Lock()
        self._next_search_at = 0.0

    def _provider(self, demo: bool) -> SearchProvider | None:
        if demo:
            return DemoProvider()
        if self.provider:
            return self.provider
        if self.settings.search_provider == "brave":
            return BraveSearchProvider(self.settings.brave_search_api_key)
        if self.settings.search_provider == "searxng":
            return SearxngProvider(self.settings.searxng_url)
        if self.settings.search_provider == "auto":
            return SearxngProvider(self.settings.searxng_url) if self.settings.searxng_url else None
        raise ValueError("SEARCH_PROVIDER must be auto, brave or searxng")

    async def _search(self, provider: SearchProvider, query: str, demo: bool) -> list[SearchHit]:
        key = hashlib.sha256(f"{type(provider).__name__}:{getattr(provider, 'base_url', '')}:{query}".encode()).hexdigest()
        cached = self.storage.cached_hits(key)
        if cached is not None:
            return cached
        if not demo and isinstance(provider, (BraveSearchProvider, SearxngProvider)):
            # Conservative single-process pacing; production replicas need a shared quota.
            async with self._rate_lock:
                delay = self._next_search_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next_search_at = time.monotonic() + 1.1
        hits = await provider.search(query, self.settings.max_results_per_query)
        self.storage.save_hits(key, hits, self.settings.result_cache_hours)
        return hits

    async def run(self, request: SearchRequest) -> IdentityReport:
        if request.demo and (request.name != "Maya Chen" or request.company != "Cedar Utilities"
                             or request.company_domain != "cedar.example.org"):
            raise ValueError("Demo uses fictional Maya Chen at cedar.example.org only")
        provider = self._provider(request.demo)
        queries = []
        warnings = []
        hits_by_url = {}
        pages = {}
        if not request.demo and request.company_domain:
            try:
                company_hits, pages, discovery_warnings = await self.company_discoverer(
                    request.name, request.company_domain, self.storage)
                warnings.extend(discovery_warnings)
                queries.append(f"{request.name} on {request.company_domain} (first-party crawl)")
                for hit in company_hits:
                    hits_by_url.setdefault(canonical_url(hit.url), hit)
            except Exception as exc:
                warnings.append(f"Company site discovery failed ({type(exc).__name__}).")
        if provider is None:
            warnings.append("Broader web search is unavailable; " +
                            ("only the supplied company site was checked." if request.company_domain
                             else "configure SearXNG to discover name-only candidates."))
        for query in query_plan(request) if provider else []:
            queries.append(query)
            try:
                hits = await self._search(provider, query, request.demo)
                for hit in hits:
                    hits_by_url.setdefault(canonical_url(hit.url), hit)
            except Exception as exc:
                warnings.append(f"Search query failed ({type(exc).__name__}); results may be incomplete.")
        hits = list(hits_by_url.values())
        company_urls = [hit.url for hit in sorted(
            (item for item in hits if is_company_url(item.url, request.company_domain)),
            key=lambda item: company_candidate_rank(item, request.name), reverse=True)][:4]
        github_urls = [hit.url for hit in hits if github_username(hit.url)][:2]
        profiles = {}
        for url in company_urls:
            if url in pages:
                continue
            try:
                pages[url] = ("Maya Chen leads partner technology at Cedar Utilities. maya@cedar.example.org"
                              if request.demo else await (
                                  self.document_fetcher(url, request.company_domain)
                                  if url.lower().split("?", 1)[0].endswith(".pdf")
                                  else self.company_fetcher(url, request.company_domain)))
                if url.lower().split("?", 1)[0].endswith(".pdf") and pages[url] == "":
                    warnings.append("An official PDF has no extractable text; OCR or human review is required.")
            except Exception as exc:
                warnings.append(f"Company page could not be checked ({type(exc).__name__}).")
        for url in github_urls:
            try:
                if request.demo:
                    profiles[url] = {"name": "Maya Chen", "company": "Cedar Utilities",
                                     "bio": "Utility technology", "email": None}
                    continue
                cached_profile = self.storage.cached_value(f"github-profile:{url}")
                profiles[url] = (cached_profile if cached_profile is not None
                                 else await self.github_fetcher(url, self.settings.github_token))
                if cached_profile is None and profiles[url] is not None:
                    self.storage.save_value(f"github-profile:{url}", profiles[url], 24)
            except Exception as exc:
                warnings.append(f"GitHub profile could not be checked ({type(exc).__name__}).")
        site_index = self.storage.cached_value(f"site-index:{request.company_domain}") if request.company_domain else None
        social_accounts = [OfficialAccount.model_validate(item) for item in (site_index or {}).get("social_accounts", [])]
        evaluated = [evaluate_hit(request, hit, pages.get(hit.url), profiles.get(hit.url), social_accounts) for hit in hits]
        evidence = sorted(
            (item for item in evaluated if any(signal.startswith("full name ") for signal in item.signals)
             or "short name linked to surname by official email" in item.signals),
            key=lambda item: item.score, reverse=True,
        )
        status, explanation = resolve_status(
            evidence, name_only=not (request.company or request.company_domain or request.work_email))
        if request.work_email and not any("exact work email publicly listed" in item.signals for item in evidence):
            warnings.append("Work email was not publicly confirmed; mailbox ownership and deliverability are untested.")
        if not evidence and not warnings:
            warnings.append("No result named this person; this does not prove the person does not exist.")
        report = IdentityReport(
            run_id=str(uuid4()), name=request.name, company=request.company,
            company_domain=request.company_domain, work_email=request.work_email,
            status=status, explanation=explanation, evidence=evidence,
            queries=queries, warnings=warnings,
        )
        self.storage.save_report(report)
        self.storage.purge_older_than(30)
        return report
