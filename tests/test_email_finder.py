import asyncio
import time

import httpx

from deepsearch.config import Settings
from deepsearch.email_finder import HostPacer, deobfuscate, local_matches_name, rank_documents, retry_after_seconds, script_emails
from deepsearch.engine import SearchEngine
from deepsearch.models import SearchHit
from deepsearch.organizations import OrganizationEngine, OrganizationRequest, extract_contacts, same_person
from deepsearch.providers import KeylessWebSearchProvider, SearchChain
from deepsearch.storage import Storage


def test_obfuscated_and_script_addresses_are_decoded_without_false_positives():
    text, found = deobfuscate("Email jsmith [at] cedar [dot] org or MARY AT cedar DOT org. Visit us at cedar.org; meet at noon.")
    assert found == ["jsmith@cedar.org", "mary@cedar.org"]
    assert "us@cedar.org" not in text
    assert script_emails("<script>document.write('rlopez' + '@' + 'cedar.org')</script>") == ["rlopez@cedar.org"]


def test_prose_binding_requires_the_address_to_match_the_name():
    text = "Questions? Jane Smith Billing Manager can be reached at jsmith@cedar.org. Robert Lopez handles press: media@cedar.org"
    rows = extract_contacts("", text, "https://cedar.org/minutes.pdf")
    people = {row["email"]: row["name"] for row in rows if row["kind"] == "person"}
    assert people == {"jsmith@cedar.org": "Jane Smith"}
    assert next(row for row in rows if row["email"] == "media@cedar.org")["kind"] == "role_mailbox"
    assert local_matches_name("jsmith@cedar.org", "Jane Smith") and not local_matches_name("billing@cedar.org", "Jane Smith")


def test_documents_ranked_by_directory_hints_and_name():
    urls = ["https://cedar.org/files/menu.pdf", "https://cedar.org/docs/staff-directory.pdf", "https://cedar.org/about"]
    assert rank_documents(urls) == ["https://cedar.org/docs/staff-directory.pdf"]


def test_named_contact_found_on_public_third_party_page_only_with_strict_match(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path))
    engine = SearchEngine(Settings(organization_keyless_search=False, searxng_url="https://search.example"), storage)

    class Provider:
        async def search(self, query, limit):
            return [SearchHit(provider="searxng", query=query, title="Commission roster", url="https://psc.state.example/roster"),
                    SearchHit(provider="searxng", query=query, title="Broker", url="https://www.zoominfo.com/p/jane")]

    async def no_site_pages(name, domain, store, limit):
        return [], {}, []

    pages = {"https://psc.state.example/roster": (
        "<div>Cedar Utilities — Jane Smith, General Manager, jsmith@cedar.org</div>"
        "<div>Cedar Utilities — Jane Smith (personal) janesmith1975@gmail.com</div>"
        "<div>Cedar Utilities — Tom Reed, Finance Director, treed@cedar.org</div>")}

    async def fetch(url, store, *args, **kwargs):
        return ("html", pages[url]) if url in pages else ("blocked", "")

    monkeypatch.setattr(engine, "_provider", lambda demo: Provider())
    monkeypatch.setattr("deepsearch.organizations.discover_company_pages", no_site_pages)
    monkeypatch.setattr("deepsearch.organizations.fetch_public", fetch)
    rows, notes = asyncio.run(OrganizationEngine(engine).find_named_contact(
        OrganizationRequest(organization_name="Cedar Utilities", contact_name="Jane Smith"), "cedar.org"))
    assert [(row["name"], row["email"], row["email_evidence"]) for row in rows] == [
        ("Jane Smith", "jsmith@cedar.org", "listed_on_public_third_party_page")]
    assert notes == []


def test_named_contact_reports_honestly_when_nothing_is_published(tmp_path, monkeypatch):
    storage = Storage(str(tmp_path))
    engine = SearchEngine(Settings(organization_keyless_search=False, searxng_url="https://search.example"), storage)

    class Empty:
        async def search(self, query, limit):
            return []

    async def no_site_pages(name, domain, store, limit):
        return [], {}, []
    monkeypatch.setattr(engine, "_provider", lambda demo: Empty())
    monkeypatch.setattr("deepsearch.organizations.discover_company_pages", no_site_pages)
    rows, notes = asyncio.run(OrganizationEngine(engine).find_named_contact(
        OrganizationRequest(organization_name="Cedar Utilities", contact_name="Jane Smith"), "cedar.org"))
    assert rows == [] and "no address was guessed" in notes[0]


def test_host_cooldown_persists_and_retry_after_is_honoured(tmp_path):
    pacer = HostPacer(0, 0, storage=Storage(str(tmp_path)))
    pacer.cooldown("cedar.org", retry_after_seconds(httpx.Response(429, headers={"Retry-After": "120"})))
    assert pacer.cooling_until("cedar.org") > time.time() + 100
    assert not pacer.cooling_until("other.org")


def test_keyless_search_cools_down_after_a_challenge_and_chain_falls_back(tmp_path):
    storage = Storage(str(tmp_path))

    class Challenged(KeylessWebSearchProvider):
        async def _search(self, query, limit):
            raise RuntimeError("challenge")

    keyless = Challenged(storage, interval=0, cooldown_minutes=30)
    try:
        asyncio.run(keyless.search("q", 3))
    except RuntimeError:
        pass
    assert keyless.cooling()

    class Working:
        async def search(self, query, limit):
            return [SearchHit(provider="searxng", query=query, title="t", url="https://cedar.org/")]
    assert asyncio.run(SearchChain([keyless, Working()]).search("q", 3))[0].url == "https://cedar.org/"
    assert same_person("Ms. Jane A. Smith", "Jane Smith") and not same_person("Jane Smithers", "Jane Smith")
