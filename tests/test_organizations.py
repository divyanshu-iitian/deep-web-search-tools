import asyncio
from deepsearch.config import Settings
from deepsearch.engine import SearchEngine
from deepsearch.storage import Storage
from deepsearch.organizations import extract_contacts, OrganizationEngine, OrganizationRequest, organization_matches, classify_organization, owns_website
from deepsearch.providers import _PublicResults


def test_staff_cards_are_bound_to_their_own_email():
    html='''<div><h2>Maya Chen</h2><p>Billing Director</p><a href="mailto:maya@cedar.org">Email</a></div>
    <div><h2>John Rowan</h2><p>Finance Director</p><a href="mailto:john@cedar.org">Email</a></div>
    <p>Customer support support@cedar.org</p>'''
    contacts=extract_contacts(html,'Maya Chen Billing Director John Rowan Finance Director support@cedar.org','https://cedar.org/team')
    assert {x['name']:x['email'] for x in contacts if x['kind']=='person'}=={'Maya Chen':'maya@cedar.org','John Rowan':'john@cedar.org'}
    assert next(x for x in contacts if x['email']=='support@cedar.org')['identity_status']=='review'


def test_jsonld_person_and_no_guessed_email():
    html='''<script type="application/ld+json">{"@type":"Person","name":"Alex Rowan","jobTitle":"General Manager","email":"alex@cedar.org"}</script>'''
    assert extract_contacts(html,'','https://cedar.org/team')[0]['name']=='Alex Rowan'
    assert extract_contacts('<p>Jane Smith General Manager</p>','Jane Smith General Manager','https://cedar.org/team')==[]


def test_multi_email_directory_does_not_assign_people():
    text='Maya Chen Billing Director John Rowan Finance Director maya@cedar.org john@cedar.org'
    rows=extract_contacts('<div>'+text+'</div>',text,'https://cedar.org/team')
    assert all(x['identity_status']=='review' for x in rows)


def test_organization_name_and_geography_are_checked(tmp_path,monkeypatch):
    storage=Storage(str(tmp_path));engine=SearchEngine(Settings(organization_keyless_search=False),storage)
    async def registry(*args):return ['https://wrong.org','https://cedar.org']
    async def crawl(name,domain,store,limit):
        text='Wrong Company Florida' if domain=='wrong.org' else 'Cedar Utilities provides water in Florida to 12,000 customers.'
        store.save_value('site-html-v1:https://'+domain+'/',f'<title>{"Wrong Company" if domain=="wrong.org" else "Cedar Utilities"}</title>',24)
        return [],{'https://'+domain+'/':text},[]
    monkeypatch.setattr('deepsearch.organizations.discover_company_pages',crawl)
    result=asyncio.run(OrganizationEngine(engine,registry).run(OrganizationRequest(organization_name='Cedar Utilities',state='FL')))
    assert result['company_domain']=='cedar.org' and result['geography_status']=='supported'
    assert result['facts']['customers_served']['value']=='12,000'
    other=asyncio.run(OrganizationEngine(engine,registry).run(OrganizationRequest(organization_name='Cedar Utilities',state='GA')))
    assert other['status']=='company_review' and other['facts']['in_scope']['value']=='Review'


def test_absent_sources_do_not_invent_company_facts():
    assert not organization_matches('Cedar Utilities','Pine Utilities')
    facts=classify_organization(OrganizationRequest(organization_name='Cedar Utilities'),' ',{})
    assert 'customers_served' not in facts


def test_department_heading_is_not_a_person_and_name_after_role_is_read():
    html='<div>Animal Services Division Manager Ashleigh Renz 951.413.3790<a href="mailto:ashleighr@moval.org">Email</a></div>'
    people=extract_contacts(html,'Animal Services Division Manager Ashleigh Renz','https://moval.org/team')
    assert people[0]['name']=='Ashleigh Renz'
    html='<div>Animal Services Division Manager <a href="mailto:someone@moval.org">Email</a></div>'
    assert all(x['identity_status']=='review' for x in extract_contacts(html,'','https://moval.org/team'))


def test_empty_href_and_public_email_obfuscation():
    address='maya@cedar.org';key=23;encoded=bytes([key]+[ord(x)^key for x in address]).hex()
    html=f'<div>Maya Chen Billing Director<a href>Email</a><span data-cfemail="{encoded}">Email</span></div>'
    assert extract_contacts(html,'','https://cedar.org/team')[0]['email']==address

def test_mentions_on_other_organization_site_do_not_prove_ownership():
    assert not owns_website('Southeastern Connecticut Council of Governments (SCCOG)',
        '<title>Norwich City</title>','Norwich works with Southeastern Connecticut Council of Governments (SCCOG).')
    assert owns_website('Southeastern Connecticut Council of Governments (SCCOG)',
        '<title>SECOG</title>','Copyright 2026 Southeastern Connecticut Council of Governments')

def test_public_search_redirect_is_a_candidate_url_only():
    parser=_PublicResults()
    parser.feed('<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fcedar.org%2F">Cedar Utilities</a>')
    assert parser.results==[{'url':'https://cedar.org/','title':'Cedar Utilities'}]


def test_association_membership_size_is_not_its_customer_count():
    facts=classify_organization(OrganizationRequest(organization_name='APGA',record_type='Association'),
        'APGA has over 730 members. Small systems under 500 meters pay dues.',
        {'https://apga.org':'APGA has over 730 members. Small systems under 500 meters pay dues.'})
    assert 'customers_served' not in facts and facts['member_count']['value']=='730'
