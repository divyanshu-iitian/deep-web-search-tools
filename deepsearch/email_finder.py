"""Find publicly *published* email addresses. Nothing here constructs or guesses an address.

Coverage comes from reading what organizations publish (staff cards, plain-text bios, PDFs,
lightly obfuscated addresses) and from a targeted search for a named contact. Politeness comes
from per-host pacing, a shared fetch limit, Retry-After handling and cooldowns persisted in the
cache, so a throttled site or search engine is left alone instead of being retried in a loop.
"""
import asyncio
import ipaddress
import random
import re
import socket
import time
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_AT = r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\[@\]|\(@\))\s*"
_DOT = r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\[\.\]|\(\.\)|\s+dot\s+)\s*"
# Brackets/parentheses make "at"/"dot" unambiguous; bare words are accepted only in capitals.
OBFUSCATED = re.compile(r"\b([A-Z0-9._%+-]{2,64})" + _AT + r"([A-Z0-9-]{2,63}(?:(?:" + _DOT + r"|\.)[A-Z0-9-]{2,63})+)\b", re.I)
BARE = re.compile(r"\b([A-Za-z0-9._%+-]{2,64})\s+AT\s+([A-Za-z0-9-]{2,63}(?:\s+DOT\s+[A-Za-z0-9-]{2,63})+)\b")
SCRIPT_JOIN = re.compile(r"""['"]([A-Za-z0-9._%+-]{2,64})['"]\s*\+\s*['"](?:@|&#64;|%40)['"]\s*\+\s*['"]([A-Za-z0-9.-]+\.[A-Za-z]{2,})['"]""")
SCRIPT_VARS = re.compile(r"""(?:var|let|const)\s+(\w+)\s*=\s*['"]([A-Za-z0-9._%+-]{2,64})['"]\s*;?\s*(?:var|let|const)\s+(\w+)\s*=\s*['"]([A-Za-z0-9.-]+\.[A-Za-z]{2,})['"]\s*;?[^<]{0,200}?\b\1\s*\+\s*['"]@['"]\s*\+\s*\3\b""")
NAME = r"(?:[A-Z][a-zA-Z'’-]+\.?\s+){1,3}[A-Z][a-zA-Z'’-]+"
TITLE_WORDS = {"general", "chief", "senior", "executive", "assistant", "deputy", "vice", "acting", "interim", "finance",
               "billing", "operations", "director", "manager", "officer", "clerk", "treasurer", "engineer", "secretary"}
USER_AGENT = "DeepWebSearchTools/0.3 (+public professional research; contact via site owner)"
EXCLUDED_HOSTS = {"wikipedia.org", "linkedin.com", "facebook.com", "x.com", "twitter.com", "instagram.com",
                  "yelp.com", "zoominfo.com", "dnb.com", "bloomberg.com", "rocketreach.co", "apollo.io",
                  "signalhire.com", "contactout.com", "lusha.com", "hunter.io", "spokeo.com", "whitepages.com"}


def deobfuscate(text: str) -> tuple[str, list[str]]:
    """Rewrite published 'name [at] domain [dot] org' forms into plain addresses."""
    found = []

    def join(match, dot):
        domain = re.sub(dot, ".", match.group(2), flags=re.I).replace(" ", "")
        address = f"{match.group(1)}@{domain}".lower()
        if EMAIL.fullmatch(address):
            found.append(address)
            return address
        return match.group(0)

    text = OBFUSCATED.sub(lambda m: join(m, _DOT), unescape(text))
    text = BARE.sub(lambda m: join(m, r"\s+DOT\s+"), text)
    return text, list(dict.fromkeys(found))


def script_emails(html: str) -> list[str]:
    """Addresses assembled by simple inline scripts to hide them from naive scrapers."""
    found = [f"{user}@{domain}".lower() for user, domain in SCRIPT_JOIN.findall(html or "")]
    found += [f"{match.group(2)}@{match.group(4)}".lower() for match in SCRIPT_VARS.finditer(html or "")]
    return [x for x in dict.fromkeys(found) if EMAIL.fullmatch(x)]


def local_matches_name(email: str, name: str) -> bool:
    """The mailbox local part must contain a meaningful part of the person's name."""
    local = re.sub(r"[^a-z]", "", email.split("@", 1)[0].lower())
    parts = [x for x in re.findall(r"[a-z]+", name.lower()) if len(x) >= 3]
    if any(x in local for x in parts):
        return True
    # Common initial forms such as jsmith / smithj, still anchored on the full surname.
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        return local in {first[0] + last, last + first[0], first + last[0]}
    return False


def text_contacts(text: str, source_url: str, roles: re.Pattern, human_name, supplied_name: str = "",
                  window: int = 220) -> list[dict]:
    """Bind an address to a real name in plain text (PDF tables, prose bios, minutes).

    The nearest name *before* the address wins (names lead in rows and sentences); a following
    name is used only when none precedes. Another address between them, or a local part that does
    not match the name, means no binding: the address stays an unattributed review candidate."""
    rows = []
    for match in EMAIL.finditer(text):
        email = match.group(0).lower().rstrip(".")
        start, end = max(0, match.start() - window), min(len(text), match.end() + window)
        context, at = text[start:end], match.start() - start
        names = []  # (start, end, name) relative to context
        if supplied_name:
            for item in re.finditer(re.escape(supplied_name), context, re.I):
                names.append((item.start(), item.end(), supplied_name))
        for item in re.finditer(NAME, context):
            # A greedy capitalized run like "Jane Smith Billing Manager" holds the name as a sub-span.
            tokens = list(re.finditer(r"\S+", item.group(0)))
            for size in (3, 2):
                for index in range(len(tokens) - size + 1):
                    first, last = tokens[index], tokens[index + size - 1]
                    candidate = item.group(0)[first.start():last.end()].strip(" .,")
                    if human_name(candidate) and not TITLE_WORDS & set(candidate.lower().split()):
                        names.append((item.start() + first.start(), item.start() + first.start() + len(candidate), candidate))
        before = [x for x in names if x[1] <= at]
        after = [x for x in names if x[0] >= at + len(match.group(0))]
        chosen = max(before, key=lambda x: (x[1], x[1] - x[0])) if before else min(after, key=lambda x: x[0]) if after else None
        if not chosen:
            continue
        low, high = (chosen[1], at) if chosen[1] <= at else (at + len(match.group(0)), chosen[0])
        name = chosen[2]
        if not local_matches_name(email, name) or EMAIL.search(context[low:high]):
            continue
        role_hits = list(roles.finditer(context))
        role = min(role_hits, key=lambda x: abs(x.start() - at)).group(0) if role_hits else ""
        rows.append({"name": name, "title": role, "email": email, "source_url": source_url,
                     "background": context.strip()[:1000], "email_evidence": "publicly_listed_in_text"})
    return rows


class HostPacer:
    """Polite crawling: one request at a time per host, spaced with jitter, plus cooldowns."""

    def __init__(self, interval: float = 1.5, jitter: float = 1.0, concurrency: int = 6, storage=None):
        self.interval, self.jitter, self.storage = interval, jitter, storage
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = None
        self._concurrency = concurrency

    def attach(self, storage):
        self.storage = storage
        return self

    def cooling_until(self, host: str) -> float:
        value = self.storage.cached_value(f"host-cooldown:{host}") if self.storage else None
        return float(value) if value and float(value) > time.time() else 0.0

    def cooldown(self, host: str, seconds: float):
        seconds = max(60.0, min(seconds, 6 * 3600.0))
        if self.storage:
            self.storage.save_value(f"host-cooldown:{host}", time.time() + seconds, max(1, int(seconds // 3600) + 1))

    async def wait(self, host: str):
        if self._global is None:
            self._global = asyncio.Semaphore(self._concurrency)
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            delay = self._next.get(host, 0.0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next[host] = time.monotonic() + self.interval + random.uniform(0, self.jitter)

    def slot(self):
        if self._global is None:
            self._global = asyncio.Semaphore(self._concurrency)
        return self._global


from deepsearch.config import settings as _settings  # noqa: E402 - settings only tune politeness

PACER = HostPacer(_settings.crawl_interval_seconds, _settings.crawl_jitter_seconds)


def retry_after_seconds(response: httpx.Response, default: float = 900.0) -> float:
    value = response.headers.get("retry-after", "")
    if value.isdigit():
        return float(value)
    try:
        return max(60.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, IndexError):
        return default


async def public_host(host: str) -> bool:
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)


async def fetch_public(url: str, storage, pacer: HostPacer = PACER, byte_limit: int = 650_000,
                       redirects: int = 0) -> tuple[str, str]:
    """Fetch one public HTTPS page or PDF after robots, IP, pacing and cooldown checks.

    Returns (kind, content): kind is html, pdf_text, skipped or blocked."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in {None, 443}
            or any(host == x or host.endswith("." + x) for x in EXCLUDED_HOSTS)):
        return "skipped", ""
    cached = storage.cached_value(f"public-fetch-v1:{url}")
    if cached:
        return cached["kind"], cached["content"]
    if pacer.cooling_until(host) or not await public_host(host):
        return "blocked", ""
    async with pacer.slot():
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False,
                                     headers={"User-Agent": USER_AGENT}) as client:
            robots_text = storage.cached_value(f"robots-v1:{host}")
            if robots_text is None:
                await pacer.wait(host)
                response = await client.get(f"https://{host}/robots.txt")
                if response.status_code in {401, 403, 429}:
                    if response.status_code == 429:
                        pacer.cooldown(host, retry_after_seconds(response))
                    return "blocked", ""
                robots_text = response.text if response.status_code == 200 else ""
                storage.save_value(f"robots-v1:{host}", robots_text, 24)
            robots = RobotFileParser()
            robots.parse(robots_text.splitlines())
            if robots_text and not robots.can_fetch(USER_AGENT, url):
                return "blocked", ""
            await pacer.wait(host)
            async with client.stream("GET", url) as response:
                if response.status_code in {429, 503}:
                    pacer.cooldown(host, retry_after_seconds(response))
                    return "blocked", ""
                location = response.headers.get("location", "")
                if response.status_code in {301, 302, 303, 307, 308}:
                    redirect = urljoin(url, location)
                elif response.status_code != 200:
                    return "skipped", ""
                else:
                    redirect = ""
                    content_type = response.headers.get("content-type", "").lower()
                    limit = 3_000_000 if "pdf" in content_type else byte_limit
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            return "skipped", ""
                        chunks.append(chunk)
    if redirect:
        return await fetch_public(redirect, storage, pacer, byte_limit, redirects + 1) if redirects < 3 else ("skipped", "")
    body = b"".join(chunks)
    if "application/pdf" in content_type:
        kind, content = "pdf_text", await asyncio.to_thread(pdf_text, body)
    elif "html" in content_type or "text/plain" in content_type:
        kind, content = "html", body.decode("utf-8", errors="replace")
    else:
        return "skipped", ""
    storage.save_value(f"public-fetch-v1:{url}", {"kind": kind, "content": content}, 24)
    return kind, content


def pdf_text(data: bytes, pages: int = 15) -> str:
    from io import BytesIO
    from pypdf import PdfReader
    try:
        reader = PdfReader(BytesIO(data))
        return re.sub(r"\s+", " ", " ".join((page.extract_text() or "") for page in reader.pages[:pages])).strip()[:60_000]
    except Exception:  # noqa: BLE001 - malformed public PDFs are skipped, never fatal
        return ""


PDF_HINTS = ("staff", "directory", "contact", "personnel", "organization", "org-chart", "orgchart", "annual",
             "report", "board", "commission", "council", "roster", "leadership", "management", "budget")


def rank_documents(urls: list[str], name: str = "") -> list[str]:
    def score(url):
        path = urlparse(url).path.lower()
        tokens = [x for x in re.findall(r"[a-z]+", name.lower()) if len(x) > 2]
        return 10 * sum(x in path for x in tokens) + 3 * sum(x in path for x in PDF_HINTS) - path.count("/")
    pdfs = [x for x in dict.fromkeys(urls) if urlparse(x).path.lower().endswith(".pdf")]
    return [x for x in sorted(pdfs, key=score, reverse=True) if score(x) > 0]
