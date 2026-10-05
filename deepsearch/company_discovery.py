"""Keyless, bounded discovery on the supplied company's public website."""
import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import httpx

from deepsearch.identity import contains_phrase
from deepsearch.models import SearchHit
from deepsearch.official_social import account_from_homepage_link
from deepsearch.sources import is_company_url
from deepsearch.storage import Storage


USER_AGENT = "DeepWebSearchTools/0.2 (+public professional research)"
PAGE_LIMIT = 10
BYTE_LIMIT = 650_000
TEXT_LIMIT = 80_000
PATH_HINTS = ("team", "people", "leadership", "about", "staff", "bio", "profile", "board")


class _Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.links = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def page_text(html: str) -> str:
    page = _Page()
    page.feed(html)
    return re.sub(r"\s+", " ", " ".join(page.text)).strip()[:TEXT_LIMIT]


async def _get(client: httpx.AsyncClient, url: str, domain: str) -> tuple[int, str]:
    if not is_company_url(url, domain):
        raise ValueError("URL is outside supplied company domain")
    host = urlparse(url).hostname
    addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Company host must resolve only to public IP addresses")
    async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
        if response.status_code in {403, 404, 410, 429}:
            return response.status_code, ""
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not any(kind in content_type for kind in ("text/html", "text/plain", "xml")):
            return response.status_code, ""
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > BYTE_LIMIT:
                raise ValueError("Company page exceeds byte limit")
            chunks.append(chunk)
        return response.status_code, b"".join(chunks).decode("utf-8", errors="replace")


def _candidate(url: str, domain: str) -> bool:
    path = urlparse(url).path.lower()
    return (is_company_url(url, domain) and
            not re.search(r"\.(?:pdf|jpg|png|gif|svg|zip|js|css|mp4)$", path) and
            len(path) < 250)


def _score(url: str, name: str) -> int:
    path = urlparse(url).path.lower()
    tokens = [part for part in re.findall(r"[a-z]+", name.lower()) if len(part) > 2]
    return (15 * sum(token in path for token in tokens) +
            5 * sum(hint in path for hint in PATH_HINTS) - path.count("/"))


async def discover_company_pages(name: str, domain: str | None, storage: Storage,
                                 page_limit: int = PAGE_LIMIT) -> tuple[list[SearchHit], dict[str, str], list[str]]:
    if not domain:
        return [], {}, ["Company domain is required for keyless first-party discovery."]
    warnings = []
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
        cached = storage.cached_value(f"site-index:{domain}")
        if cached:
            base, candidates, robots_text = cached["base"], cached["urls"], cached["robots"]
        else:
            base, homepage = None, ""
            for origin in (f"https://{domain}", f"https://www.{domain}"):
                try:
                    status, html = await _get(client, origin, domain)
                    if status == 200 and html:
                        base, homepage = origin, html
                        break
                except (httpx.HTTPError, OSError, ValueError):
                    continue
            if not base:
                return [], {}, ["Company homepage could not be fetched; keyless discovery was incomplete."]
            robots_text = ""
            try:
                robots_status, robots_text = await _get(client, base + "/robots.txt", domain)
                if robots_status == 403:
                    return [], {}, ["Company robots.txt was denied; site crawl skipped."]
                if robots_status == 429:
                    return [], {}, ["Company site rate limited robots.txt; site crawl paused."]
            except (httpx.HTTPError, OSError, ValueError):
                warnings.append("Robots rules could not be checked; site crawl skipped.")
                return [], {}, warnings
            robots = RobotFileParser()
            robots.parse(robots_text.splitlines())
            if not robots.can_fetch(USER_AGENT, base + "/"):
                return [], {}, ["Company robots.txt disallows this crawler."]
            page = _Page()
            page.feed(homepage)
            social_accounts = []
            for href in page.links:
                account = account_from_homepage_link(urljoin(base + "/", href), domain, base)
                if account:
                    social_accounts.append(account.model_dump(mode="json"))
            candidates = [base]
            candidates.extend(urljoin(base + "/", href) for href in page.links)
            sitemaps = re.findall(r"(?im)^sitemap:\s*(\S+)", robots_text)[:3]
            if not sitemaps:
                sitemaps = [base + "/sitemap.xml"]
            for sitemap in sitemaps:
                if not _candidate(sitemap, domain):
                    continue
                try:
                    status, xml = await _get(client, sitemap, domain)
                    if status != 200 or not xml:
                        continue
                    root = ElementTree.fromstring(xml)
                    locations = [node.text.strip() for node in root.iter()
                                 if node.tag.endswith("loc") and node.text]
                    # One bounded sitemap index level; large sites remain deliberately incomplete.
                    if root.tag.endswith("sitemapindex"):
                        for nested in locations[:2]:
                            if _candidate(nested, domain):
                                try:
                                    _, nested_xml = await _get(client, nested, domain)
                                    nested_root = ElementTree.fromstring(nested_xml)
                                    candidates.extend(node.text.strip() for node in nested_root.iter()
                                                      if node.tag.endswith("loc") and node.text)
                                except (httpx.HTTPError, OSError, ValueError, ElementTree.ParseError):
                                    pass
                    else:
                        candidates.extend(locations)
                except (httpx.HTTPError, OSError, ValueError, ElementTree.ParseError):
                    warnings.append("A company sitemap could not be read.")
            candidates = list(dict.fromkeys(url.split("#", 1)[0] for url in candidates
                                            if _candidate(url, domain)))[:1500]
            storage.save_value(f"site-index:{domain}", {"base": base, "urls": candidates,
                                                         "robots": robots_text,
                                                         "social_accounts": social_accounts}, 24)

        robots = RobotFileParser()
        robots.parse(robots_text.splitlines())
        ranked = sorted(candidates, key=lambda url: _score(url, name), reverse=True)
        if base in ranked:
            ranked.remove(base)
            ranked.insert(0, base)
        hits, pages = [], {}
        for url in ranked[:page_limit]:
            if not robots.can_fetch(USER_AGENT, url):
                continue
            cache_key = f"site-page-v2:{url}"
            body = storage.cached_value(cache_key)
            if body is None:
                try:
                    status, html = await _get(client, url, domain)
                    if status == 429:
                        warnings.append("Company site returned 429; remaining page checks were paused.")
                        break
                    if status != 200 or not html:
                        continue
                    body = page_text(html)
                    storage.save_value(cache_key, body, 24)
                except (httpx.HTTPError, OSError, ValueError):
                    warnings.append("A candidate company page could not be checked.")
                    continue
            if contains_phrase(body, name):
                at = body.casefold().find(name.casefold())
                excerpt = body[max(0, at - 160):at + len(name) + 240] if at >= 0 else body[:400]
                hits.append(SearchHit(provider="company_site", query=f"{name} site:{domain}",
                                      title=f"Company page: {urlparse(url).path or '/'}",
                                      url=url, snippet=excerpt))
                pages[url] = body
        return hits, pages, warnings
