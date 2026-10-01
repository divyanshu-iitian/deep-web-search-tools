"""Search API adapters. Search snippets are leads, never identity proof."""
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import httpx

from deepsearch.models import SearchHit


class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, limit: int) -> list[SearchHit]:
        raise NotImplementedError


class BraveSearchProvider(SearchProvider):
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("BRAVE_SEARCH_API_KEY is required")
        self.api_key = api_key

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                self.endpoint,
                params={"q": query, "count": min(limit, 20), "safesearch": "strict"},
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
        return _hits("brave", query, data.get("web", {}).get("results", []), "description")


class SearxngProvider(SearchProvider):
    def __init__(self, base_url: str):
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("SEARXNG_URL must be an HTTP(S) origin")
        self.base_url = base_url.rstrip("/")

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "safesearch": 2},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
        if not data.get("results") and data.get("unresponsive_engines"):
            raise RuntimeError("SearXNG upstream engines were unavailable")
        return _hits("searxng", query, data.get("results", [])[:limit], "content")


class DemoProvider(SearchProvider):
    """Fictional result set for UI testing; never used for a real query by default."""

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        return [
            SearchHit(provider="demo", query=query, title="Maya Chen | Cedar Utilities",
                      url="https://cedar.example.org/team/maya-chen",
                      snippet="Maya Chen leads partner technology at Cedar Utilities."),
            SearchHit(provider="demo", query=query, title="Maya Chen on GitHub",
                      url="https://github.com/maya-cedar-demo",
                      snippet="Maya Chen, Cedar Utilities, utility technology."),
        ][:limit]


def _hits(provider: str, query: str, items: list[dict], snippet_key: str) -> list[SearchHit]:
    results = []
    seen = set()
    for item in items:
        try:
            hit = SearchHit(
                provider=provider, query=query, title=item.get("title") or "",
                url=item.get("url") or "", snippet=item.get(snippet_key) or "",
            )
        except ValueError:
            continue
        if hit.url not in seen:
            results.append(hit)
            seen.add(hit.url)
    return results
