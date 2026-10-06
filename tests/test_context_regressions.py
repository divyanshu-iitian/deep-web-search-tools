import asyncio
from deepsearch.config import Settings
from deepsearch.engine import SearchEngine
from deepsearch.models import SearchRequest, SearchHit
from deepsearch.identity import evaluate_hit
from deepsearch.storage import Storage


def test_supplied_page_is_not_lost_when_company_crawl_returns_empty(tmp_path):
    calls=[]
    async def page(url,domain):
        calls.append(url)
        return 'Maya Chen works at Cedar Utilities. maya@cedar.example.org'
    async def discover(*_):return [],{},[]
    engine=SearchEngine(Settings(data_dir=str(tmp_path)),Storage(str(tmp_path)),
                        company_fetcher=page,company_discoverer=discover)
    request=SearchRequest(name='Maya Chen',company='Cedar Utilities',company_domain='cedar.example.org',
                          work_email='maya@cedar.example.org',source_url='https://cedar.example.org/team')
    first=asyncio.run(engine.run(request))
    second=asyncio.run(engine.run(request))
    assert first.status.value=='supported' and second.status.value=='supported'
    assert len(calls)==1


def test_someone_elses_email_on_same_directory_is_not_a_match():
    request=SearchRequest(name='Maya Chen',company='Cedar Utilities',company_domain='cedar.example.org',
                          work_email='other@cedar.example.org')
    hit=SearchHit(provider='test',query='Maya Chen',title='Team',url='https://cedar.example.org/team')
    page='Maya Chen leads utility software. '+(' unrelated content '*80)+'John Doe other@cedar.example.org'
    evidence=evaluate_hit(request,hit,company_page=page)
    assert 'full name on company page' in evidence.signals
    assert 'exact work email publicly listed' not in evidence.signals
