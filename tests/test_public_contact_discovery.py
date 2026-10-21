import asyncio

from deepsearch.config import Settings
from deepsearch.engine import SearchEngine
from deepsearch.models import SearchHit
from deepsearch.organizations import OrganizationEngine, OrganizationRequest
from deepsearch.storage import Storage


def test_company_only_search_reads_directory_and_pdf_without_known_person(tmp_path, monkeypatch):
    engine = SearchEngine(Settings(), Storage(str(tmp_path)))
    provider = object()
    monkeypatch.setattr(OrganizationEngine, 'search_provider', lambda self: provider)
    urls = ['https://state.gov/utility-directory.pdf', 'https://cedar.org/staff',
            'https://broker.zoominfo.com/person', 'https://state.gov/irrelevant']
    queries, fetched = [], []

    async def search(selected, query, demo):
        assert selected is provider and not demo
        queries.append(query)
        return [SearchHit(provider='test', query=query, title='Utility directory', url=url,
                          snippet='Unfetched Person Director unfetched@cedar.org') for url in urls]

    async def fetch(url, storage):
        fetched.append(url)
        if url.endswith('.pdf'):
            return 'pdf_text', 'Maya Chen, Billing Director. Email: maya.chen@cedar.org'
        if url.endswith('/staff'):
            return 'html', '<div>John Rowan Finance Director <a href="mailto:john.rowan@cedar.org">Email</a></div>'
        return 'html', '<div>Wrong Person Finance Director wrong.person@other.org</div><p>info@cedar.org</p>'

    monkeypatch.setattr(engine, '_search', search)
    monkeypatch.setattr('deepsearch.organizations.fetch_public', fetch)
    rows, _ = asyncio.run(OrganizationEngine(engine).find_public_contacts(
        OrganizationRequest(organization_name='Cedar Utilities', state='FL'), 'cedar.org'))
    assert {row['email'] for row in rows} == {'maya.chen@cedar.org', 'john.rowan@cedar.org'}
    assert all(row['identity_status'] == 'supported' and row['source_url'] in fetched for row in rows)
    assert not any('zoominfo' in url for url in fetched)
    assert any('filetype:pdf' in query for query in queries)


def test_named_search_never_substitutes_another_person(tmp_path, monkeypatch):
    engine = SearchEngine(Settings(), Storage(str(tmp_path)))
    monkeypatch.setattr(OrganizationEngine, 'search_provider', lambda self: object())

    async def search(*args):
        return [SearchHit(provider='test', query='test', title='Directory', url='https://state.gov/directory')]

    async def fetch(*args):
        return 'html', '<div>John Rowan Finance Director john.rowan@cedar.org</div>'

    monkeypatch.setattr(engine, '_search', search)
    monkeypatch.setattr('deepsearch.organizations.fetch_public', fetch)
    rows, notes = asyncio.run(OrganizationEngine(engine).find_public_contacts(
        OrganizationRequest(organization_name='Cedar Utilities', contact_name='Maya Chen'), 'cedar.org'))
    assert rows == [] and notes


def test_finance_bio_sentence_is_not_a_person():
    from deepsearch.organizations import leaders, human_name
    assert leaders({'https://cedar.org/team':'Director of Financial Accounting. She joined Cedar Utilities in 2019.'}) == []
    assert not human_name('Jimenez Financial Resources')
    assert leaders({'https://apga.org':'Blog Entry Job Open, President'}) == []


def test_slow_sitemap_preserves_allowed_homepage_evidence(tmp_path,monkeypatch):
    from deepsearch import company_discovery
    async def fetch(client,url,domain):
        if url.endswith('/robots.txt'):return 200,''
        if url.endswith('/sitemap.xml'):
            await asyncio.sleep(1)
        return 200,'<title>Cedar Utilities</title><p>Cedar Utilities water services in Florida.</p>'
    monkeypatch.setattr(company_discovery,'_get',fetch)
    hits,pages,notes=asyncio.run(company_discovery.discover_company_pages('', 'cedar.org',Storage(str(tmp_path)),time_budget=0.05))
    assert hits and 'Cedar Utilities' in pages['https://cedar.org']
    assert notes


def test_blocked_homepage_can_use_fetched_company_listing_not_snippet(tmp_path,monkeypatch):
    engine=SearchEngine(Settings(),Storage(str(tmp_path)))
    monkeypatch.setattr(OrganizationEngine,'search_provider',lambda self:object())
    async def search(*args):
        return [SearchHit(provider='test',query='test',title='Cedar Utilities',url='https://state.gov/directory',
                          snippet='Cedar Utilities website: https://wrong.org')]
    async def fetch(*args):
        return 'html',('<article>Cedar Utilities, Florida. Website: <a href="https://cedar.org">Cedar Utilities</a>'
                       '<div>Maya Chen Billing Director maya.chen@cedar.org</div></article>')
    monkeypatch.setattr(engine,'_search',search)
    monkeypatch.setattr('deepsearch.organizations.fetch_public',fetch)
    result=asyncio.run(OrganizationEngine(engine).public_listing(OrganizationRequest(organization_name='Cedar Utilities',state='FL')))
    assert result['domain']=='cedar.org' and result['url']=='https://state.gov/directory'
    async def no_source(*args):return 'blocked',''
    monkeypatch.setattr('deepsearch.organizations.fetch_public',no_source)
    assert asyncio.run(OrganizationEngine(engine).public_listing(OrganizationRequest(organization_name='Cedar Utilities',state='FL'))) is None


def test_public_listing_rejects_multiple_websites_and_wrong_state(tmp_path,monkeypatch):
    engine=SearchEngine(Settings(),Storage(str(tmp_path)))
    monkeypatch.setattr(OrganizationEngine,'search_provider',lambda self:object())
    async def search(*args):
        return [SearchHit(provider='test',query='test',title='Directory',url='https://state.gov/directory')]
    async def fetch(*args):
        return 'html','<article>Cedar Utilities Florida website <a href="https://cedar.org">One</a><a href="https://cedarwater.org">Two</a></article>'
    monkeypatch.setattr(engine,'_search',search)
    monkeypatch.setattr('deepsearch.organizations.fetch_public',fetch)
    for state in ('FL','GA'):
        assert asyncio.run(OrganizationEngine(engine).public_listing(OrganizationRequest(organization_name='Cedar Utilities',state=state))) is None


def test_public_pdf_can_establish_company_domain_and_named_address(tmp_path,monkeypatch):
    engine=SearchEngine(Settings(),Storage(str(tmp_path)))
    monkeypatch.setattr(OrganizationEngine,'search_provider',lambda self:object())
    async def search(*args):
        return [SearchHit(provider='test',query='test',title='Utilities',url='https://state.gov/directory.pdf')]
    async def fetch(*args):
        return 'pdf_text','Cedar Utilities Florida. Website: https://cedar.org Maya Chen Billing Director maya.chen@cedar.org'
    monkeypatch.setattr(engine,'_search',search)
    monkeypatch.setattr('deepsearch.organizations.fetch_public',fetch)
    result=asyncio.run(OrganizationEngine(engine).public_listing(OrganizationRequest(organization_name='Cedar Utilities',state='FL')))
    assert result['domain']=='cedar.org' and result['html']==''
    from deepsearch.organizations import extract_contacts
    assert extract_contacts('',result['text'],result['url'])[0]['email']=='maya.chen@cedar.org'


def test_company_only_run_recovers_contact_when_homepage_is_blocked(tmp_path,monkeypatch):
    engine=SearchEngine(Settings(),Storage(str(tmp_path)))
    monkeypatch.setattr(OrganizationEngine,'search_provider',lambda self:object())
    async def search(provider,query,demo):
        return [SearchHit(provider='test',query=query,title='Cedar Utilities',url='https://state.gov/directory')]
    async def blocked(*args):return [],{},['Company homepage could not be fetched.']
    async def fetch(*args):
        return 'html',('<article>Cedar Utilities water services in Florida. Website: <a href="https://cedar.org">Cedar Utilities</a>'
                       '<div>Maya Chen Billing Director maya.chen@cedar.org</div></article>')
    monkeypatch.setattr(engine,'_search',search)
    monkeypatch.setattr('deepsearch.organizations.discover_company_pages',blocked)
    monkeypatch.setattr('deepsearch.organizations.fetch_public',fetch)
    result=asyncio.run(OrganizationEngine(engine).run(OrganizationRequest(organization_name='Cedar Utilities',state='FL')))
    assert result['company_domain']=='cedar.org' and result['status']=='completed'
    person=next(row for row in result['contacts'] if row['email']=='maya.chen@cedar.org')
    assert person['name']=='Maya Chen' and person['source_url']=='https://state.gov/directory'
    assert person['identity_status']=='supported'
    assert any('fetched public directory' in note for note in result['warnings'])
