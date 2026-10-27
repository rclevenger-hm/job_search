"""Small reviewed careers watchlist; page content is parsed as data, never executed."""
import html
import json
import re
import time
import urllib.request
from urllib.error import HTTPError
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from core import ROLE, US, Links, greenhouse, eligibility

COMPANIES = [
    dict(id='canonical',name='Canonical',url='https://canonical.com/careers/all',kind='page',default=True),
    dict(id='automattic',name='Automattic',url='https://automattic.com/work-with-us/jobs/',kind='page',default=True),
    dict(id='gitlab',name='GitLab',url='https://job-boards.greenhouse.io/gitlab',kind='api',default=True),
    dict(id='grafanalabs',name='Grafana Labs',url='https://job-boards.greenhouse.io/grafanalabs',kind='api',default=True),
    dict(id='cloudflare',name='Cloudflare',url='https://www.cloudflare.com/careers/',kind='api',default=True),
    dict(id='verrus',name='Verrus',url='https://www.verrusdata.com/careers',kind='page',default=False),
]

def read_page(url, host):
    # All starting URLs are reviewed constants; detail links stay on that host.
    class SameHost(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urlparse(newurl).scheme!='https' or urlparse(newurl).hostname!=host:
                raise ValueError('Careers page redirected outside approved host')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname!=host or parsed.username or parsed.port not in (None,443):
        raise ValueError('Unapproved careers URL')
    request=urllib.request.Request(url,headers={'User-Agent':'JobRadar/1.0 personal job alerts'})
    with urllib.request.build_opener(SameHost()).open(request,timeout=20) as response:
        raw=response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024: raise ValueError('Careers page too large')
    return raw.decode('utf-8')

def text(value):
    return ' '.join(html.unescape(re.sub('<[^>]+>',' ',value or '')).split())

def array_variable(page, variable):
    match=re.search(r'\bconst\s+'+re.escape(variable)+r'\s*=\s*',page)
    if not match: raise ValueError('Careers listing format changed')
    value,_=json.JSONDecoder().raw_decode(page[match.end():])
    if not isinstance(value,list): raise ValueError('Invalid careers listing')
    return value

def job_posting(page):
    def walk(value):
        if isinstance(value,list):
            for item in value: yield from walk(item)
        elif isinstance(value,dict):
            if value.get('@type')=='JobPosting': yield value
            yield from walk(value.get('@graph',[]))
    for raw in re.findall(r'<script[^>]+type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>',page,re.S|re.I):
        try:
            for item in walk(json.loads(raw)): return item
        except ValueError: continue
    return {}

def parse_detail(page, url, company, title, key=None, location=''):
    data=job_posting(page)
    heading=re.search(r'<h[12][^>]*>(.*?)</h[12]>',page,re.S|re.I)
    title=data.get('title') or title or (text(heading[1]) if heading else '')
    if not ROLE.search(title): return None
    if location and eligibility(location,True)=='Excluded': return None
    requirements=data.get('applicantLocationRequirements') or []
    if isinstance(requirements,dict): requirements=[requirements]
    countries=[r.get('name','') for r in requirements if isinstance(r,dict)]
    if countries and not any(US.search(c) for c in countries): return None
    remote=data.get('jobLocationType')=='TELECOMMUTE'
    if not location:
        # Only explicit role-specific remote statements, not a site footer or tech stack.
        body=text(data.get('description') or page)
        match=re.search(r'(?:This (?:role|position|job) is|Location\s*:)\s*(?:a\s+)?(?:fully\s+)?remote[^.]{0,150}',body,re.I)
        if match: location=match[0]; remote=True
    remote=remote or bool(re.search(r'home[ -]based|\bremote\b',location,re.I))
    if not remote: return None
    loc='United States; remote' if countries else location
    if eligibility(loc,True)!='Ready': return None
    posted=data.get('datePosted') or ''
    try:
        stamp=datetime.fromisoformat(posted.replace('Z','+00:00'))
        if stamp.tzinfo is None: posted=''
    except (ValueError,TypeError): posted=''
    expires=time.time()+86400
    if data.get('validThrough'):
        try:
            end=datetime.fromisoformat(data['validThrough'].replace('Z','+00:00'))
            if end.tzinfo: expires=min(expires,end.timestamp())
        except (ValueError,TypeError): pass
    if expires<=time.time(): return None
    body=text(data.get('description') or page)
    email=re.search(r'(?:apply|submit|send)[^.]{0,180}?([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})',body,re.I)
    method='Email: '+email[1] if email else 'Online form' if data.get('directApply') is True or '<form' in page.lower() else 'See employer instructions'
    return dict(key=key or 'careers:'+url,title=title,company=company,location=loc,url=url,
        source='Curated careers: '+company,eligibility='Ready',posted=posted,
        expires=expires,source_date='Application: '+method)

def monitor(company):
    if company['kind']=='api':
        rows=greenhouse(company['id'])
        for row in rows:
            row['company']=company['name']
            row['source_date']='Application: Online form (employer board)'
        return [r for r in rows if r['eligibility']=='Ready']
    host=urlparse(company['url']).hostname
    page=read_page(company['url'],host)
    candidates=[]
    if company['id']=='canonical':
        for j in array_variable(page,'vacancies'):
            if ROLE.search(j.get('title','')):
                candidates.append((f"https://canonical.com/careers/{j['id']}",j['title'],f"gh:canonical:{j['id']}",j.get('location','')))
    elif company['id']=='automattic':
        for j in array_variable(page,'ghJobsData'):
            if ROLE.search(j.get('title','')):
                candidates.append((j['href'],j['title'],f"gh:automatticcareers:{j['id']}",''))
    else:
        links=Links(); links.feed(page)
        for href,title in links.links:
            url=urljoin(company['url'],href)
            if ROLE.search(title) and urlparse(url).hostname==host:
                candidates.append((url,title,None,''))
    results=[]
    for url,title,key,location in list(dict.fromkeys(candidates))[:20]:
        try: detail=read_page(url,host)
        except HTTPError as exc:
            if exc.code in (404,410): continue
            raise
        job=parse_detail(detail,url,company['name'],title,key,location)
        if job: results.append(job)
    return results
