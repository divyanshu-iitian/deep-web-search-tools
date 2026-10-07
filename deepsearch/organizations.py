"""Organization -> verified website -> public contact candidates, never guessed emails."""
import asyncio
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin, unquote

import httpx
from pydantic import BaseModel, Field

from deepsearch.company_discovery import discover_company_pages
from deepsearch.identity import contains_phrase
from deepsearch.models import SearchHit
from deepsearch.sources import is_company_url
from deepsearch.providers import KeylessWebSearchProvider

EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
ROLE = re.compile(r"\b(?:chief(?:\s+(?:executive|financial|information|technology|operating))?\s+officer|(?:vice\s+)?president(?: and CEO)?|CEO|CFO|CIO|general manager|(?:(?:utility|utilities|water|electric|gas|finance|financial|billing|information|technology|IT|operations|engineering|executive|managing|assistant|deputy|public works)\s+){0,3}(?:director|manager)|administrator|superintendent)\b", re.I)
GENERIC = {"info", "contact", "support", "customerservice", "customer.service", "billing", "office", "admin", "sales", "service", "media", "press", "webmaster", "outage", "emergency", "noreply", "no-reply"}
NON_PERSON_WORDS={'division','department','services','service','city','county','office','staff','contact','email','utility','utilities','public','support','water','billing','information','director','team','administration','finance','communications','community','planning','works','customer','board','council','member','membership','association','about','development'}
NAME_PATTERN=r"(?:[A-Z][a-zA-Z'’-]+\.?\s+){1,3}[A-Z][a-zA-Z'’-]+\.?"

def human_name(value):
    return bool(re.fullmatch(NAME_PATTERN,value) and not (set(re.findall(r'\w+',value.lower())) & (NON_PERSON_WORDS|{'superintendent','administrator','manager','president','ceo','cfo','cio'})))
EXCLUDED_HOSTS = {"wikipedia.org", "wikidata.org", "linkedin.com", "facebook.com", "x.com", "twitter.com", "yelp.com", "zoominfo.com", "dnb.com", "bloomberg.com"}
STATES=dict(x.split(':',1) for x in 'AL:Alabama|AK:Alaska|AZ:Arizona|AR:Arkansas|CA:California|CO:Colorado|CT:Connecticut|DE:Delaware|FL:Florida|GA:Georgia|HI:Hawaii|ID:Idaho|IL:Illinois|IN:Indiana|IA:Iowa|KS:Kansas|KY:Kentucky|LA:Louisiana|ME:Maine|MD:Maryland|MA:Massachusetts|MI:Michigan|MN:Minnesota|MS:Mississippi|MO:Missouri|MT:Montana|NE:Nebraska|NV:Nevada|NH:New Hampshire|NJ:New Jersey|NM:New Mexico|NY:New York|NC:North Carolina|ND:North Dakota|OH:Ohio|OK:Oklahoma|OR:Oregon|PA:Pennsylvania|RI:Rhode Island|SC:South Carolina|SD:South Dakota|TN:Tennessee|TX:Texas|UT:Utah|VT:Vermont|VA:Virginia|WA:Washington|WV:West Virginia|WI:Wisconsin|WY:Wyoming|DC:District of Columbia'.split('|'))


class OrganizationRequest(BaseModel):
    organization_name: str = Field(min_length=2, max_length=255)
    website: str = Field(default="", max_length=2000)
    state: str = Field(default="", max_length=80)
    county: str = Field(default="", max_length=120)
    country: str = Field(default="US", max_length=80)
    record_type: str = Field(default="ICP", max_length=80)
    contact_name: str = Field(default="", max_length=160)
    max_pages: int = Field(default=12, ge=1, le=20)


def organization_matches(name, text):
    if contains_phrase(text, name):
        return True
    expanded = re.sub(r'\([^)]*\)','',name)
    tokens = [x for x in re.findall(r"[a-z0-9]+", expanded.casefold()) if x not in {
        "city", "of", "the", "inc", "llc", "ltd", "company", "co", "annual", "conference", "and", "exhibition", "exposition", "sys", "dept"}]
    tokens = list(dict.fromkeys(tokens))
    return bool(tokens and all(re.search(r"\b"+re.escape(x)+r"\b", text.casefold()) for x in tokens))


class BrandingHTML(HTMLParser):
    def __init__(self):super().__init__();self.depth=0;self.values=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in {'title','h1'}:self.depth+=1
        if tag=='meta' and attrs.get('property')=='og:site_name':self.values.append(attrs.get('content') or '')
        if tag=='img' and 'logo' in ((attrs.get('class') or '')+' '+(attrs.get('src') or '')).lower():self.values.append(attrs.get('alt') or '')
    def handle_endtag(self,tag):
        if tag in {'title','h1'} and self.depth:self.depth-=1
    def handle_data(self,data):
        if self.depth:self.values.append(data)

def owns_website(name,html,text):
    parser=BrandingHTML();parser.feed(html or '')
    signals=' '.join(parser.values)
    signals+=' '+' '.join(re.findall(r'(?:copyright|©)\s*.{0,200}',text,re.I))
    return organization_matches(name,signals)


class ContactHTML(HTMLParser):
    """Keep each staff card or table row separate instead of flattening a directory."""
    def __init__(self):
        super().__init__()
        self.stack = []
        self.blocks = []
        self.jsonld = []
        self.script = None
        self.hidden = 0
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        encoded=attrs.get('data-cfemail')
        href=attrs.get('href') or ''
        if not encoded and '/cdn-cgi/l/email-protection#' in href:
            encoded=href.split('#',1)[-1]
        if encoded:
            try:
                data=bytes.fromhex(encoded)
                decoded=bytes(x^data[0] for x in data[1:]).decode('utf-8')
                if EMAIL.fullmatch(decoded):
                    self.links.append('mailto:'+decoded)
                    for block in self.stack:block['links'].append('mailto:'+decoded)
            except (ValueError,UnicodeDecodeError,IndexError):pass
        if tag == 'script':
            self.script = '' if attrs.get('type') == 'application/ld+json' else None
            self.hidden += 1
        if tag in {'style','noscript'}:
            self.hidden += 1
        if tag in {'div','li','tr','article','section','p','address'}:
            self.stack.append({'tag':tag,'text':[],'links':[]})
        if tag == 'a' and attrs.get('href'):
            href = unquote(attrs['href']).strip()
            self.links.append(href)
            for block in self.stack:
                block['links'].append(href)

    def handle_data(self, data):
        if self.script is not None:
            self.script += data
        if not self.hidden:
            for block in self.stack:
                block['text'].append(data)

    def handle_endtag(self, tag):
        if tag == 'script':
            if self.script:
                try:self.jsonld.append(json.loads(self.script))
                except ValueError:pass
            self.script = None
        if tag in {'script','style','noscript'} and self.hidden:
            self.hidden -= 1
        if self.stack and self.stack[-1]['tag'] == tag:
            block = self.stack.pop()
            text = re.sub(r'\s+',' ',' '.join(block['text'])).strip()
            if text and len(text) <= 1800:
                self.blocks.append((text,block['links']))


def _json_people(value):
    if isinstance(value,list):
        for child in value:yield from _json_people(child)
    elif isinstance(value,dict):
        types=value.get('@type',[])
        if types=='Person' or isinstance(types,list) and 'Person' in types:
            yield value
        for child in value.values():
            if isinstance(child,(dict,list)):yield from _json_people(child)


def rank_contact(title, generic=False,context=''):
    title=title.lower()
    if generic:return 0
    if re.search(r'\b(?:animal services|animal shelter|recreation|police|fire department|library|building permits)\b',context,re.I):return 10
    if title in {'manager','director'} and not re.search(r'\b(?:utility|utilities|water|gas|electric|billing|finance|information|technology)\b',context,re.I):return 25
    if any(x in title for x in ('media','communications','public relations','human resources','recruit','customer service')):return 10
    if any(x in title for x in ('billing','information','technology','finance','general manager','utility director','utilities director','executive director','president','chief','ceo','cfo','cio','administrator','superintendent')):return 90
    return 55 if title else 25


def extract_contacts(html, text, source_url, supplied_name=''):
    parser=ContactHTML();parser.feed(html or '')
    rows=[]
    def add(name,title,email,context,phone='',linkedin='',kind='person'):
        emails=EMAIL.findall(email)
        if not emails:return
        address=emails[0].lower()
        generic=address.split('@')[0] in GENERIC
        if generic:kind='role_mailbox'
        rows.append({'name':name,'title':title[:160],'email':address,'phone':phone[:80],
            'linkedin_url':linkedin,'source_url':source_url,'background':context[:1000],
            'kind':kind,'relevance_score':rank_contact(title,generic,context),
            'email_evidence':'publicly_listed','identity_status':'supported' if name and not generic else 'review'})
    for person in _json_people(parser.jsonld):
        name=str(person.get('name',''))
        if len(name.split())>=2:
            add(name,str(person.get('jobTitle','')),str(person.get('email','')),json.dumps(person,ensure_ascii=False),
                str(person.get('telephone','')),next((x for x in person.get('sameAs',[]) if 'linkedin.com/in/' in x),'') if isinstance(person.get('sameAs'),list) else '')
    for block,links in sorted(parser.blocks,key=lambda x:len(x[0])):
        emails=list(dict.fromkeys(EMAIL.findall(block+' '+' '.join(x for x in links if x.lower().startswith('mailto:')))))
        if len(emails)!=1:continue  # Never pair a directory-wide email with one person's name.
        roles=list(ROLE.finditer(block))
        email_at=block.lower().find(emails[0].lower())
        email_at=email_at if email_at>=0 else len(block)
        role=min(roles,key=lambda x:abs(x.start()-email_at)) if roles else None
        name=supplied_name if supplied_name and contains_phrase(block,supplied_name) else ''
        if not name:
            direct=list(re.finditer(r'\b(?:contact|email)\s*:?\s*('+NAME_PATTERN+r')',block))
            direct=[x for x in direct if human_name(x.group(1))]
            if direct:name=min(direct,key=lambda x:abs(x.start()-email_at)).group(1)
        if not name and role:
            prefix=block[:role.start()].strip(' ,|:-')
            match=re.search(r'(?:^|[|:])\s*('+NAME_PATTERN+r')$',prefix)
            if match and human_name(match.group(1).strip()):name=match.group(1).strip()
            if not name:
                match=re.match(r'\s*[:,-]?\s*('+NAME_PATTERN+r')',block[role.end():])
                if match and human_name(match.group(1).strip()):name=match.group(1).strip()
        if not name:continue
        local=emails[0].split('@',1)[0].lower()
        name_parts=re.findall(r'[a-z]+',name.lower())
        if not any(len(x)>=3 and x in local for x in name_parts):continue
        # Tiny blocks are preferred; broader duplicated cards lose to these by email.
        phone=re.search(r'(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?:\s*(?:ext\.?|x)\s*\d+)?',block)
        linkedin=next((urljoin(source_url,x) for x in links if 'linkedin.com/in/' in x),'')
        add(name,role.group(0).strip() if role else '',emails[0],block,phone.group(0) if phone else '',linkedin)
        if rows and re.search(r'\b(?:for immediate release|media contact|press releases)\b',block,re.I):
            rows[-1]['relevance_score']=min(rows[-1]['relevance_score'],10)
    # Unknown-name and general addresses remain review candidates; never invent a person.
    all_emails=list(dict.fromkeys(EMAIL.findall(text+' '+' '.join(x for x in parser.links if x.lower().startswith('mailto:')))))
    for email in all_emails:
        if not any(x['email']==email.lower() for x in rows):
            position=text.lower().find(email.lower());snippet=text[max(0,position-100):position+180] if position>=0 else 'Public mailto link on source page.'
            add('','',email,snippet,kind='unattributed')
    seen={}
    for row in rows:
        if row['email'] not in seen or row['relevance_score']>seen[row['email']]['relevance_score']:
            seen[row['email']]=row
    return sorted(seen.values(),key=lambda x:(-x['relevance_score'],x['email']))


async def wikipedia_websites(name, state):
    """Free public entity registry fallback; website claims still need direct site checks."""
    headers={'User-Agent':'BynryResearch/0.1 (https://github.com/divyanshu-iitian/deep-web-search-tools; public organization research)'}
    async with httpx.AsyncClient(timeout=12,trust_env=False,headers=headers) as client:
        response=await client.get('https://en.wikipedia.org/w/api.php',params={
            'action':'query','format':'json','generator':'search','gsrsearch':name+' '+state,
            'gsrlimit':3,'prop':'pageprops'})
        response.raise_for_status()
        entities=[page.get('pageprops',{}).get('wikibase_item') for page in response.json().get('query',{}).get('pages',{}).values()]
        ids=[x for x in entities if x and re.fullmatch(r'Q\d+',x)]
        if not ids:return []
        response=await client.get('https://www.wikidata.org/w/api.php',params={
            'action':'wbgetentities','format':'json','ids':'|'.join(ids),'props':'claims'})
        response.raise_for_status()
        return [claim.get('mainsnak',{}).get('datavalue',{}).get('value','')
            for entity in response.json().get('entities',{}).values() for claim in entity.get('claims',{}).get('P856',[])][:5]


class OrganizationEngine:
    def __init__(self, search_engine, registry=wikipedia_websites):
        self.search_engine=search_engine
        self.registry=registry

    async def run(self, request: OrganizationRequest):
        started=time.monotonic();warnings=[];candidates=[]
        if request.website:candidates=[request.website]
        else:
            provider=self.search_engine._provider(False)
            if not provider and self.search_engine.settings.organization_keyless_search:
                provider=KeylessWebSearchProvider()
            if provider:
                try:
                    hits=await self.search_engine._search(provider,
                        f'"{request.organization_name}" {request.state} {request.county} official website',False)
                    candidates=[x.url for x in hits]
                except (httpx.HTTPError,RuntimeError,ValueError):warnings.append('Broad search provider unavailable for this organization.')
            if not candidates:
                try:candidates=await self.registry(request.organization_name,request.state)
                except (httpx.HTTPError,ValueError):warnings.append('Free public entity registry unavailable.')
            if not provider:warnings.append('No broad search provider configured; free entity registry was used. Long-tail organizations may need SearXNG or a supplied website.')
        website='';pages={};domain='';evidence=[];geography='unknown'
        for candidate in list(dict.fromkeys(candidates))[:5]:
            parsed=urlparse(candidate if '://' in candidate else 'https://'+candidate)
            if parsed.scheme=='http':parsed=parsed._replace(scheme='https')
            host=(parsed.hostname or '').lower().removeprefix('www.')
            if parsed.scheme!='https' or not host or parsed.username or parsed.password or parsed.port not in {None,443} or any(host==x or host.endswith('.'+x) for x in EXCLUDED_HOSTS):continue
            hits,bodies,notes=await discover_company_pages('',host,self.search_engine.storage,request.max_pages)
            if not bodies: warnings.extend(notes);continue
            combined=' '.join(bodies.values())
            if not organization_matches(request.organization_name,combined):
                warnings.append(f'Candidate {host} did not support the organization name.');continue
            homepage=next((url for url in bodies if urlparse(url).path in {'','/'}),next(iter(bodies)))
            html=self.search_engine.storage.cached_value(f'site-html-v1:{homepage}') or ''
            if not owns_website(request.organization_name,html,bodies[homepage]):
                warnings.append(f'Candidate {host} mentions the organization but does not establish website ownership.');continue
            state=request.state.upper()
            matched_state = state not in STATES or contains_phrase(combined,STATES[state]) or bool(re.search(r'\b'+re.escape(state)+r'\s+\d{5}(?:-\d{4})?\b',combined))
            if website and not matched_state:continue
            website=f'https://{parsed.netloc}';domain=host;pages=bodies;warnings.extend(notes)
            geography='supported' if matched_state else 'unknown'
            evidence=[{'url':url,'excerpt':body[:1000]} for url,body in pages.items()]
            if matched_state:break
        contacts=[]
        for url,text in pages.items():
            html=self.search_engine.storage.cached_value(f'site-html-v1:{url}') or ''
            contacts.extend(extract_contacts(html,text,url,request.contact_name))
        unique={}
        for row in sorted(contacts,key=lambda x:-x['relevance_score']):
            unique.setdefault(row['email'],row)
        contacts=list(unique.values())[:50]
        for row in contacts:
            email_domain=row['email'].rsplit('@',1)[-1]
            if email_domain!=domain and not email_domain.endswith('.'+domain):
                row['identity_status']='review';row['relevance_score']=min(row['relevance_score'],40)
        text=' '.join(pages.values())
        facts=classify_organization(request,text,pages)
        if website and geography=='unknown':
            warnings.append('Organization name matched, but supplied state was not supported by page text; review geographic identity.')
            facts['in_scope']={'value':'Review','source_url':'','quote':'Geographic identity needs confirmation before assessing fit.'}
        return {'organization_name':request.organization_name,'website':website,'company_domain':domain,
            'status':('completed' if geography=='supported' else 'company_review') if website else 'needs_website','geography_status':geography,'contacts':contacts,'facts':facts,
            'evidence':evidence,'warnings':list(dict.fromkeys(warnings)),
            'time_spent_min':round((time.monotonic()-started)/60,2)}


def classify_organization(request,text,pages):
    """Observations with a literal excerpt and URL; blanks represent missing evidence."""
    facts={}
    patterns={
        'service_type':r'\b(electric(?:ity)?|natural gas|water|wastewater|sewer)\b',
        'ownership':r'\b(cooperative|municipally owned|municipal(?: water| electric| gas)? utilit(?:y|ies)|investor[- ]owned|publicly owned)\b',
        'association_type':r'\b(trade association|water association|municipal association|utility association|electric cooperative association)\b',
        'customers_served':r'\b([\d,]+)\s+(?:electric |water |gas |utility )?(?:customers|connections|meters|accounts)\b',
        'member_count':r'\b([\d,]+)\s+(?:utility |association )?members\b',
        'billing_system':r'\b(?:billing system|customer information system|CIS)(?:\s+(?:is|from|by|called))?\s+([A-Z][\w-]+(?:\s+[A-Z][\w-]+){0,2})',
        'has_member_directory':r'\b(member directory|membership directory)\b',
    }
    for field,pattern in patterns.items():
        if field=='customers_served' and request.record_type.lower()=='association':
            continue  # Member utility size bands are not the association's own customer count.
        for url,body in pages.items():
            match=re.search(pattern,body,re.I if field!='billing_system' else 0)
            if match:
                if field=='customers_served' and re.search(r'\b(?:under|between|pricing|dues|eligible|typically|designed for)\b',body[max(0,match.start()-80):match.start()],re.I):
                    continue
                value=match.group(1) if match.lastindex else 'Yes'
                facts[field]={'value':value,'source_url':url,'quote':body[max(0,match.start()-80):match.end()+120]}
                break
    # Fit is a decision, not a scraped fact; association records are assessed separately.
    utility=bool(re.search(r'\b(?:utility|utilities|electric|water|natural gas|wastewater)\b',text,re.I))
    count=facts.get('customers_served',{}).get('value','').replace(',','')
    if not text:fit='Unknown';reason='Official website has not been supported.'
    elif request.record_type.lower()=='association':
        fit='Yes' if utility else 'Review';reason='Utility-sector association may provide relevant industry access.' if utility else 'Association relevance to utility billing needs review.'
    elif not utility:fit='Review';reason='Official pages do not establish a utility operating context.'
    elif count.isdigit() and not 3000<=int(count)<=100000:
        fit='Review';reason='Published customer count lies outside the reference 3,000-100,000 range; confirm size and fit.'
    else:fit='Yes';reason='Official pages support a utility context; billing/CIS relevance and size require review before outreach.'
    facts['in_scope']={'value':fit,'source_url':'','quote':reason}
    return facts
