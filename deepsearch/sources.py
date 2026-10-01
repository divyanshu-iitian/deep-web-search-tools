"""Fetch only a contact's stated company site and GitHub's public user API."""
import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def is_company_url(url: str, company_domain: str | None) -> bool:
    if not company_domain:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    domain = company_domain.lower().removeprefix("www.")
    return parsed.scheme == "https" and (host == domain or host.endswith("." + domain))


async def company_page_text(url: str, company_domain: str | None) -> str | None:
    if not is_company_url(url, company_domain):
        return None
    host = urlparse(url).hostname
    addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        return None
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", url, headers={"User-Agent": "DeepWebSearchTools/0.1"}) as response:
            response.raise_for_status()
            if "text/html" not in response.headers.get("content-type", ""):
                return None
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 600_000:
                    return None
                chunks.append(chunk)
    parser = _Text()
    parser.feed(b"".join(chunks).decode("utf-8", errors="replace"))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:12_000]


def github_username(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    path = parsed.path.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", path):
        return None
    if path.lower() in {"topics", "orgs", "search", "features", "settings", "login"}:
        return None
    return path


async def github_public_profile(url: str, token: str = "") -> dict | None:
    username = github_username(url)
    if not username:
        return None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "DeepWebSearchTools/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
        response = await client.get(f"https://api.github.com/users/{username}", headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
    if data.get("type") != "User":
        return None
    return {key: data.get(key) for key in ("login", "name", "company", "bio", "email", "html_url")}
