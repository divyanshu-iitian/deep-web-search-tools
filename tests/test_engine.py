import asyncio

import pytest
from pydantic import ValidationError

from deepsearch.config import Settings
from deepsearch.engine import SearchEngine, query_plan
from deepsearch.models import IdentityStatus, SearchHit, SearchRequest
from deepsearch.providers import SearchProvider
from deepsearch.sources import github_username, is_company_url
from deepsearch.storage import Storage


class FakeProvider(SearchProvider):
    def __init__(self, hits):
        self.hits = hits
        self.calls = 0

    async def search(self, query, limit):
        self.calls += 1
        return [SearchHit(**{**hit, "query": query}) for hit in self.hits][:limit]


def fixture_engine(tmp_path, provider, company_text=None, github_profile=None):
    async def page(url, domain):
        return company_text

    async def github(url, token):
        return github_profile

    return SearchEngine(Settings(data_dir=str(tmp_path)), Storage(str(tmp_path)), provider, page, github)


def test_input_and_query_budget():
    with pytest.raises(ValidationError):
        SearchRequest(name="Rahul Sharma")
    with pytest.raises(ValidationError):
        SearchRequest(name="Rahul", company="Example")
    request = SearchRequest(name="Maya Chen", company="Cedar Utilities",
                            company_domain="cedar.example.org", work_email="maya@cedar.example.org")
    assert len(query_plan(request)) == 3
    assert is_company_url("https://team.cedar.example.org/maya", request.company_domain)
    assert not is_company_url("https://cedar.example.org.evil.test/maya", request.company_domain)
    assert github_username("https://github.com/mayac") == "mayac"
    assert github_username("https://github.com/org/repo") is None


def test_company_page_and_independent_profile_corroborate(tmp_path):
    hits = [
        {"provider": "fake", "title": "Maya Chen - Cedar Utilities",
         "url": "https://cedar.example.org/team/maya", "snippet": "Maya Chen leads partners at Cedar Utilities."},
        {"provider": "fake", "title": "Maya Chen on GitHub",
         "url": "https://github.com/mayac", "snippet": "Maya Chen at Cedar Utilities."},
    ]
    provider = FakeProvider(hits)
    engine = fixture_engine(tmp_path, provider,
                            "Maya Chen leads partners at Cedar Utilities. maya@cedar.example.org",
                            {"name": "Maya Chen", "company": "Cedar Utilities", "bio": "Utilities",
                             "email": None})
    request = SearchRequest(name="Maya Chen", company="Cedar Utilities",
                            company_domain="cedar.example.org", work_email="maya@cedar.example.org")
    report = asyncio.run(engine.run(request))
    assert report.status == IdentityStatus.corroborated
    assert any("exact work email publicly listed" in item.signals for item in report.evidence)
    assert engine.storage.get_report(report.run_id) == report
    first_calls = provider.calls
    second = asyncio.run(engine.run(request))
    assert second.status == report.status
    assert provider.calls == first_calls  # Cached query results.


def test_same_name_at_wrong_company_does_not_verify(tmp_path):
    provider = FakeProvider([
        {"provider": "fake", "title": "Maya Chen - Other Corporation",
         "url": "https://other.example.org/maya", "snippet": "Maya Chen at Other Corporation."}
    ])
    engine = fixture_engine(tmp_path, provider)
    report = asyncio.run(engine.run(SearchRequest(name="Maya Chen", company="Cedar Utilities")))
    assert report.status == IdentityStatus.unresolved
    assert report.evidence[0].score < 55


def test_demo_is_explicit_and_fictional(tmp_path):
    engine = SearchEngine(Settings(data_dir=str(tmp_path)), Storage(str(tmp_path)))
    request = SearchRequest(name="Maya Chen", company="Cedar Utilities",
                            company_domain="cedar.example.org", work_email="maya@cedar.example.org", demo=True)
    report = asyncio.run(engine.run(request))
    assert report.status == IdentityStatus.corroborated
    with pytest.raises(ValueError):
        asyncio.run(engine.run(SearchRequest(name="Rahul Sharma", company="Example", demo=True)))
