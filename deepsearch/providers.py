"""Search API adapters. Search snippets are leads, never identity proof."""
from abc import ABC, abstractmethod
from urllib.parse import urlparse, parse_qs, urljoin
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser
import time

import httpx

from deepsearch.models import SearchHit


class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, limit: int) -> list[SearchHit]:
        raise NotImplementedError


class _PublicResults(HTMLParser):
    def __init__(self):
        super().__init__();self.results=[];self.current=None
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='a' and 'result__a' in attrs.get('class','').split():
            url=urljoin('https://html.duckduckgo.com/',attrs.get('href',''))
            url=parse_qs(urlparse(url).query).get('uddg',[url])[0]
            self.current={'url':url,'title':''}
    def handle_data(self,text):
        if self.current:self.current['title']+=text
    def handle_endtag(self,tag):
        if tag=='a' and self.current:self.results.append(self.current);self.current=None


class KeylessWebSearchProvider(SearchProvider):
    """Public HTML results when allowed by robots; stop on challenges and throttling.

    Queries are spaced (with jitter) across the whole process, and a challenge or 429 puts the
    engine into a cooldown shared through the cache, so no job keeps hammering it."""
    base_url='https://html.duckduckgo.com'
    _robots=None
    _robots_until=0
    _next_at=0.0
    _lock=None
    def __init__(self,storage=None,interval=None,cooldown_minutes=None):
        from deepsearch.config import settings
        self.storage=storage
        self.interval=settings.keyless_search_interval_seconds if interval is None else interval
        self.cooldown_minutes=settings.search_cooldown_minutes if cooldown_minutes is None else cooldown_minutes
    def cooling(self):
        until=self.storage.cached_value('search-cooldown:keyless') if self.storage else None
        return bool(until and float(until)>time.time())
    def _cool(self):
        if self.storage:self.storage.save_value('search-cooldown:keyless',time.time()+self.cooldown_minutes*60,max(1,self.cooldown_minutes//60+1))
    async def _pace(self):
        import asyncio,random
        if type(self)._lock is None:type(self)._lock=asyncio.Lock()
        async with type(self)._lock:
            delay=type(self)._next_at-time.monotonic()
            if delay>0:await asyncio.sleep(delay)
            type(self)._next_at=time.monotonic()+self.interval+random.uniform(0,self.interval/2)
    async def search(self,query,limit):
        if self.cooling():
            raise RuntimeError('Public search is cooling down after throttling; skipped without retrying')
        await self._pace()
        try:
            return await self._search(query,limit)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403,429,503}:self._cool()
            raise
        except RuntimeError:
            self._cool()
            raise
    async def _search(self,query,limit):
        headers={'User-Agent':'BynryResearch/0.1 (public professional research)'}
        async with httpx.AsyncClient(timeout=15,trust_env=False,headers=headers) as client:
            if not self._robots or type(self)._robots_until<time.monotonic():
                response=await client.get(self.base_url+'/robots.txt')
                response.raise_for_status()
                robots=RobotFileParser();robots.parse(response.text.splitlines())
                type(self)._robots=robots;type(self)._robots_until=time.monotonic()+3600
            if not self._robots.can_fetch(headers['User-Agent'],self.base_url+'/html/'):
                raise RuntimeError('Public search robots policy disallows automated access')
            response=await client.get(self.base_url+'/html/',params={'q':query})
            response.raise_for_status()
            if response.status_code!=200 or 'anomaly.js' in response.text or 'challenge-form' in response.text:
                raise RuntimeError('Public search challenged or throttled this request; no bypass attempted')
            parser=_PublicResults();parser.feed(response.text)
            return _hits('public_web',query,parser.results[:limit],'snippet')


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
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
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


class SearchChain(SearchProvider):
    """Try self-hosted/configured search first, then the polite keyless fallback."""

    def __init__(self, providers: list[SearchProvider]):
        self.providers = [x for x in providers if x is not None]
        self.base_url = "+".join(type(x).__name__ for x in self.providers)

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        errors = []
        for provider in self.providers:
            if getattr(provider, "cooling", lambda: False)():
                errors.append(f"{type(provider).__name__} cooling down")
                continue
            try:
                hits = await provider.search(query, limit)
                if hits:
                    return hits
                errors.append(f"{type(provider).__name__} returned no results")
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                errors.append(f"{type(provider).__name__}: {type(exc).__name__}")
        if errors and all("no results" not in x for x in errors):
            raise RuntimeError("; ".join(errors))
        return []
