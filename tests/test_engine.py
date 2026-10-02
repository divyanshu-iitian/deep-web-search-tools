import asyncio

import pytest
from pydantic import ValidationError

from deepsearch.config import Settings
from deepsearch.engine import SearchEngine, company_candidate_rank, query_plan
from deepsearch.identity import contains_person_name, name_variants, short_name_linked_by_email
from deepsearch.models import IdentityStatus, SearchHit, SearchRequest
from deepsearch.official_social import account_from_homepage_link
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

    async def discover(name, domain, storage):
        return [], {}, []

    return SearchEngine(Settings(data_dir=str(tmp_path)), Storage(str(tmp_path)), provider, page, github, discover)


def test_input_and_query_budget():
    assert query_plan(SearchRequest(name="Dr. Manoj Kumar Chaubey")) == [
        '"manoj kumar chaubey"', '"manoj kumar choubey"']
    assert name_variants("Dr. Manoj Kumar Chaubey") == ["manoj kumar chaubey", "manoj kumar choubey"]
    with_email = SearchRequest(name="Dr. Manoj Kumar Chaubey", company="GGU",
                               company_domain="ggu.ac.in", work_email="manoj@ggu.ac.in")
    assert any("choubey" in query for query in query_plan(with_email))
    assert any("manoj@ggu.ac.in" in query for query in query_plan(with_email))
    assert contains_person_name("Dr. Manoj Kumar (Choubey)", "Dr. Manoj Kumar Chaubey")
    assert not contains_person_name("Prof. Manoj Kumar", "Dr. Manoj Kumar Chaubey")
    with pytest.raises(ValidationError):
        SearchRequest(name="Rahul", company="Example")
    request = SearchRequest(name="Maya Chen", company="Cedar Utilities",
                            company_domain="cedar.example.org", work_email="maya@cedar.example.org")
    assert len(query_plan(request)) == 3
    assert is_company_url("https://team.cedar.example.org/maya", request.company_domain)
    assert not is_company_url("https://cedar.example.org.evil.test/maya", request.company_domain)
    assert not is_company_url("https://cedar.example.org:8443/admin", request.company_domain)
    assert github_username("https://github.com/mayac") == "mayac"
    assert github_username("https://github.com/org/repo") is None


def test_official_document_resolves_spelling_variant_without_short_name_false_match(tmp_path):
    url = "https://ggu.ac.in/media/research/manoj.pdf"
    provider = FakeProvider([{"provider": "fake", "title": "Dr. Manoj Kumar (Choubey)",
                             "url": url, "snippet": "Head of IT at Guru Ghasidas Vishwavidyalaya"}])

    async def document(url, domain):
        return "Dr. Manoj Kumar (Choubey) Head of Department Department of IT Guru Ghasidas Vishwavidyalaya"

    engine = fixture_engine(tmp_path, provider)
    engine.document_fetcher = document
    report = asyncio.run(engine.run(SearchRequest(
        name="Dr. Manoj Kumar Chaubey", company="Guru Ghasidas Vishwavidyalaya",
        company_domain="ggu.ac.in")))
    assert report.status == IdentityStatus.supported
    assert report.evidence[0].category == "official_document"
    assert "full name on official document" in report.evidence[0].signals

    assert not contains_person_name("Prof. Manoj Kumar Professor at GGU", "Dr. Manoj Kumar Chaubey")


def test_short_official_name_requires_nearby_matching_institutional_email(tmp_path):
    url = "https://ggu.ac.in/media/staff/Manoj_CV.pdf"
    name = "Dr. Manoj Kumar Chaubey"
    page = ("Prof (Dr.) Manoj Kumar Professor and Head, Department of IT "
            "Guru Ghasidas Vishwavidyalaya choubey.manoj@ggu.ac.in")
    assert short_name_linked_by_email(page, name, "ggu.ac.in", url)
    assert not short_name_linked_by_email(page.replace("choubey.manoj@ggu.ac.in", "other@ggu.ac.in"),
                                          name, "ggu.ac.in", url)
    assert not short_name_linked_by_email(page, name, "other.ac.in", url)
    assert not short_name_linked_by_email(page, "Dr. Manoj Kumar Sharma", "ggu.ac.in", url)


def test_name_only_returns_candidates_without_claiming_identity(tmp_path):
    provider = FakeProvider([{
        "provider": "fake", "title": "Dr. Manoj Kumar (Choubey) at GGV",
        "url": "https://example.org/profile", "snippet": "Guru Ghasidas Vishwavidyalaya"
    }])
    report = asyncio.run(fixture_engine(tmp_path, provider).run(
        SearchRequest(name="Dr. Manoj Kumar Chaubey")))
    assert report.status == IdentityStatus.possible
    assert report.evidence[0].category == "search_result"
    assert "disambiguate" in report.explanation


def test_person_profile_ranks_above_generic_company_news():
    generic = SearchHit(provider="fake", query="q", title="News",
                        url="https://ggu.ac.in/news/innovation", snippet="Manoj Kumar")
    profile = SearchHit(provider="fake", query="q", title="Faculty",
                        url="https://ggu.ac.in/department/it/faculty/prof-manoj-kumar/",
                        snippet="Professor")
    assert company_candidate_rank(profile, "Dr. Manoj Kumar Chaubey") > company_candidate_rank(
        generic, "Dr. Manoj Kumar Chaubey")


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


def test_unrelated_results_are_not_presented_as_person_evidence(tmp_path):
    provider = FakeProvider([
        {"provider": "fake", "title": "An unrelated faculty page",
         "url": "https://unrelated.example.org/team", "snippet": "IIT Madras faculty listing"}
    ])
    report = asyncio.run(fixture_engine(tmp_path, provider).run(
        SearchRequest(name="Divyanshu Mishra", company="IIT Madras")))
    assert report.status == IdentityStatus.unresolved
    assert not report.evidence


def test_institution_listed_social_post_supports_affiliation(tmp_path):
    post = "https://www.linkedin.com/posts/iit-madras-bs-datascience-programme_iitmadras-activity-123"
    provider = FakeProvider([{
        "provider": "fake", "title": "IIT Madras BS in Data Science Programme",
        "url": post,
        "snippet": "IIT Madras BS student Divyanshu Mishra was selected at DFKI."
    }])
    report = asyncio.run(fixture_engine(tmp_path, provider).run(SearchRequest(
        name="Divyanshu Mishra", company="IIT Madras", company_domain="iitm.ac.in")))
    assert report.status == IdentityStatus.supported
    assert report.evidence[0].category == "official_social_post_indexed"
    assert report.evidence[0].authority_url.endswith("bs-degree-brochure.pdf")
    assert "search-index evidence" in report.explanation


def test_unlisted_social_account_cannot_confirm_institution(tmp_path):
    provider = FakeProvider([{
        "provider": "fake", "title": "A social post",
        "url": "https://www.linkedin.com/posts/unrelated_account-activity-123",
        "snippet": "Divyanshu Mishra IIT Madras"
    }])
    report = asyncio.run(fixture_engine(tmp_path, provider).run(SearchRequest(
        name="Divyanshu Mishra", company="IIT Madras", company_domain="iitm.ac.in")))
    assert report.status == IdentityStatus.possible
    assert report.evidence[0].category == "search_result"


def test_institution_post_without_affiliation_context_stays_possible(tmp_path):
    provider = FakeProvider([{
        "provider": "fake", "title": "IIT Madras BS in Data Science Programme",
        "url": "https://www.linkedin.com/posts/iit-madras-bs-datascience-programme_event-activity-123",
        "snippet": "Guest speaker Divyanshu Mishra answered questions at an event."
    }])
    report = asyncio.run(fixture_engine(tmp_path, provider).run(SearchRequest(
        name="Divyanshu Mishra", company="IIT Madras", company_domain="iitm.ac.in")))
    assert report.status == IdentityStatus.possible
    assert report.evidence[0].category == "search_result"


def test_institution_homepage_link_can_attribute_social_post(tmp_path):
    account = account_from_homepage_link(
        "https://www.linkedin.com/company/cedar-university/", "cedar.example.org",
        "https://cedar.example.org/")
    assert account is not None
    assert account_from_homepage_link("https://www.linkedin.com/in/a-person/",
                                      "cedar.example.org", "https://cedar.example.org/") is None
    provider = FakeProvider([{
        "provider": "fake", "title": "Cedar University update",
        "url": "https://www.linkedin.com/posts/cedar-university_maya-chen-activity-123",
        "snippet": "Maya Chen joined Cedar University as a researcher."
    }])
    engine = fixture_engine(tmp_path, provider)
    engine.storage.save_value("site-index:cedar.example.org", {
        "base": "https://cedar.example.org", "urls": [], "robots": "",
        "social_accounts": [account.model_dump(mode="json")],
    }, 24)
    report = asyncio.run(engine.run(SearchRequest(name="Maya Chen", company="Cedar University",
                                                  company_domain="cedar.example.org")))
    assert report.status == IdentityStatus.supported
    assert report.evidence[0].authority_url == "https://cedar.example.org/"


def test_demo_is_explicit_and_fictional(tmp_path):
    engine = SearchEngine(Settings(data_dir=str(tmp_path)), Storage(str(tmp_path)))
    request = SearchRequest(name="Maya Chen", company="Cedar Utilities",
                            company_domain="cedar.example.org", work_email="maya@cedar.example.org", demo=True)
    report = asyncio.run(engine.run(request))
    assert report.status == IdentityStatus.corroborated
    with pytest.raises(ValueError):
        asyncio.run(engine.run(SearchRequest(name="Rahul Sharma", company="Example", demo=True)))


def test_relevant_excerpt_uses_matched_person_and_public_email():
    from deepsearch.identity import relevant_excerpt

    page = "Company navigation " * 90 + (
        "Partner Programme Manager Joanne Lyons has channel experience. "
        + "Biography text " * 50
        + "joannelyons@fullpowerutilities.com is the published contact."
    )
    excerpt = relevant_excerpt(page, "Joanne Lyons", "joannelyons@fullpowerutilities.com")
    assert "Joanne Lyons" in excerpt
    assert "Partner Programme Manager" in excerpt
    assert "joannelyons@fullpowerutilities.com" in excerpt
    assert not excerpt.startswith("Company navigation")
