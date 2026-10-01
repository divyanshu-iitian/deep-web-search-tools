import asyncio

from deepsearch import company_discovery
from deepsearch.config import Settings
from deepsearch.engine import SearchEngine
from deepsearch.models import IdentityStatus, SearchRequest
from deepsearch.storage import Storage


def test_keyless_company_sitemap_supports_identity(tmp_path, monkeypatch):
    pages = {
        "https://cedar.example.org": (200, '<a href="/about">About</a>'),
        "https://cedar.example.org/robots.txt": (200, "User-agent: *\nDisallow: /private\nSitemap: https://cedar.example.org/sitemap.xml"),
        "https://cedar.example.org/sitemap.xml": (200, "<urlset><url><loc>https://cedar.example.org/team/maya-chen</loc></url></urlset>"),
        "https://cedar.example.org/team/maya-chen": (200, "<h1>Maya Chen</h1><p>Maya Chen leads partner technology at Cedar Utilities.</p>"),
        "https://cedar.example.org/about": (200, "<h1>About Cedar Utilities</h1>"),
    }
    requested = []

    async def fake_get(client, url, domain):
        requested.append(url)
        return pages.get(url, (404, ""))

    monkeypatch.setattr(company_discovery, "_get", fake_get)
    store = Storage(str(tmp_path))
    engine = SearchEngine(Settings(search_provider="auto", searxng_url="", data_dir=str(tmp_path)), store)
    report = asyncio.run(engine.run(SearchRequest(name="Maya Chen", company="Cedar Utilities",
                                                  company_domain="cedar.example.org")))
    assert report.status == IdentityStatus.supported
    assert report.evidence[0].category == "company_page"
    assert "https://cedar.example.org/team/maya-chen" in requested
    assert not any("/private" in url for url in requested)
    first_count = len(requested)
    asyncio.run(engine.run(SearchRequest(name="Maya Chen", company="Cedar Utilities",
                                        company_domain="cedar.example.org")))
    assert len(requested) == first_count  # Domain index and page cache work without a key.


def test_keyless_respects_robots_disallow(tmp_path, monkeypatch):
    async def fake_get(client, url, domain):
        if url.endswith("robots.txt"):
            return 200, "User-agent: *\nDisallow: /"
        return 200, "<h1>Maya Chen</h1>"

    monkeypatch.setattr(company_discovery, "_get", fake_get)
    hits, pages, warnings = asyncio.run(company_discovery.discover_company_pages(
        "Maya Chen", "cedar.example.org", Storage(str(tmp_path))))
    assert not hits and not pages
    assert any("disallows" in warning for warning in warnings)
