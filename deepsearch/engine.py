"""Bounded search → authoritative-page checks → evidence report."""
import asyncio
import hashlib
import time
from uuid import uuid4

from deepsearch.config import Settings
from deepsearch.identity import canonical_url, evaluate_hit, resolve_status
from deepsearch.models import IdentityReport, SearchHit, SearchRequest
from deepsearch.providers import BraveSearchProvider, DemoProvider, SearchProvider, SearxngProvider
from deepsearch.sources import company_page_text, github_public_profile, github_username, is_company_url
from deepsearch.storage import Storage


def query_plan(request: SearchRequest) -> list[str]:
    name = f'"{request.name}"'
    queries = []
    if request.company:
        queries.append(f'{name} "{request.company}"')
    if request.company_domain:
        queries.append(f"{name} site:{request.company_domain}")
    if request.work_email:
        queries.append(f'"{request.work_email}"')
    return list(dict.fromkeys(queries))[:3]


class SearchEngine:
    def __init__(self, settings: Settings, storage: Storage, provider: SearchProvider | None = None,
                 company_fetcher=company_page_text, github_fetcher=github_public_profile):
        self.settings = settings
        self.storage = storage
        self.provider = provider
        self.company_fetcher = company_fetcher
        self.github_fetcher = github_fetcher
        self._rate_lock = asyncio.Lock()
        self._next_search_at = 0.0

    def _provider(self, demo: bool) -> SearchProvider:
        if demo:
            return DemoProvider()
        if self.provider:
            return self.provider
        if self.settings.search_provider == "brave":
            return BraveSearchProvider(self.settings.brave_search_api_key)
        if self.settings.search_provider == "searxng":
            return SearxngProvider(self.settings.searxng_url)
        raise ValueError("SEARCH_PROVIDER must be brave or searxng")

    async def _search(self, provider: SearchProvider, query: str, demo: bool) -> list[SearchHit]:
        key = hashlib.sha256(f"{type(provider).__name__}:{query}".encode()).hexdigest()
        cached = self.storage.cached_hits(key)
        if cached is not None:
            return cached
        if not demo and isinstance(provider, BraveSearchProvider):
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
        queries = query_plan(request)
        warnings = []
        hits_by_url = {}
        for query in queries:
            try:
                hits = await self._search(provider, query, request.demo)
                for hit in hits:
                    hits_by_url.setdefault(canonical_url(hit.url), hit)
            except Exception as exc:
                warnings.append(f"Search query failed ({type(exc).__name__}); results may be incomplete.")
        hits = list(hits_by_url.values())
        company_urls = [hit.url for hit in hits if is_company_url(hit.url, request.company_domain)][:2]
        github_urls = [hit.url for hit in hits if github_username(hit.url)][:2]
        pages, profiles = {}, {}
        for url in company_urls:
            try:
                pages[url] = ("Maya Chen leads partner technology at Cedar Utilities. maya@cedar.example.org"
                              if request.demo else await self.company_fetcher(url, request.company_domain))
            except Exception as exc:
                warnings.append(f"Company page could not be checked ({type(exc).__name__}).")
        for url in github_urls:
            try:
                profiles[url] = ({"name": "Maya Chen", "company": "Cedar Utilities", "bio": "Utility technology",
                                  "email": None} if request.demo
                                 else await self.github_fetcher(url, self.settings.github_token))
            except Exception as exc:
                warnings.append(f"GitHub profile could not be checked ({type(exc).__name__}).")
        evidence = sorted(
            [evaluate_hit(request, hit, pages.get(hit.url), profiles.get(hit.url)) for hit in hits],
            key=lambda item: item.score, reverse=True,
        )
        status, explanation = resolve_status(evidence)
        if request.work_email and not any("exact work email publicly listed" in item.signals for item in evidence):
            warnings.append("Work email was not publicly confirmed; mailbox ownership and deliverability are untested.")
        if not hits and not warnings:
            warnings.append("Search returned no results; this does not prove the person does not exist.")
        report = IdentityReport(
            run_id=str(uuid4()), name=request.name, company=request.company,
            company_domain=request.company_domain, work_email=request.work_email,
            status=status, explanation=explanation, evidence=evidence,
            queries=queries, warnings=warnings,
        )
        self.storage.save_report(report)
        self.storage.purge_older_than(30)
        return report
