"""Public major-employer connectors. Missing schemas are errors, never empty success."""
import contextvars
import html
import json
import re
import time
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlparse
from core import ROLE, US, safe_url

EMPLOYERS = [
    dict(id='nvidia',name='NVIDIA',url='https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite'),
    dict(id='amazon',name='Amazon',url='https://www.amazon.jobs/en/search'),
    dict(id='google',name='Google',url='https://www.google.com/about/careers/applications/jobs/results/'),
    dict(id='apple',name='Apple',url='https://jobs.apple.com/en-us/search'),
    dict(id='microsoft',name='Microsoft',url='https://apply.careers.microsoft.com/careers'),
    dict(id='netflix',name='Netflix',url='https://explore.jobs.netflix.net/careers'),
    dict(id='meta',name='Meta',url='https://www.metacareers.com/jobs/'),
]

class ConnectorUnavailable(Exception): pass

SCAN_DEADLINE = contextvars.ContextVar("major_deadline",default=None)
CURRENT_BATCH = contextvars.ContextVar("major_batch",default=None)

class Batch(list):
    def __init__(self):
        super().__init__()
        CURRENT_BATCH.set(self)
        self.report = dict(fetched=0,matched=0,partial=False,note='')
        self.seen = set()

    def add(self, company, identity, title, locations, url, date='', verified=False):
        if not identity or identity in self.seen: return
        self.seen.add(identity)
        self.report['fetched'] += 1
        if not ROLE.search(title) or not locations or not safe_url(url): return
        posted=''
        if verified:
            try:
                stamp=datetime.fromisoformat(date.replace('Z','+00:00'))
                if stamp.tzinfo: posted=stamp.isoformat()
            except (ValueError,TypeError): pass
        self.append(dict(key='major:'+company+':'+str(identity),title=title,
            company=next(c['name'] for c in EMPLOYERS if c['id']==company),location='; '.join(locations),
            url=url,source='Direct employer: '+company,eligibility='Ready',posted=posted,
            source_date='Published: '+(date or 'Unknown')+(' (posting time unverified)' if not posted else ''),expires=0))
        self.report['matched'] += 1


def request(url, payload=None):
    deadline=SCAN_DEADLINE.get()
    if deadline is not None and time.monotonic()>=deadline: raise TimeoutError('Connector scan budget reached')
    time.sleep(.75)
    data=json.dumps(payload).encode() if payload is not None else None
    req=urllib.request.Request(url,data=data,headers={'User-Agent':'JobRadar/1.0 personal job search','Accept':'application/json,text/html','Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=20) as response:
        if urlparse(response.url).hostname != urlparse(url).hostname:
            raise ValueError('Employer redirected to an unverified host')
        raw=response.read(8*1024*1024+1)
    if len(raw)>8*1024*1024: raise ValueError('Employer response exceeds size limit')
    return raw.decode('utf-8')


def get_json(url, payload=None): return json.loads(request(url,payload))


def walk(value):
    yield value
    if isinstance(value,dict):
        for child in value.values(): yield from walk(child)
    elif isinstance(value,list):
        for child in value: yield from walk(child)


def text(value): return html.unescape(re.sub('<[^>]+>',' ',value or '')).strip()


def nvidia():
    result=Batch(); base='https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite'
    listing_seen=set(); details=0; total=0
    for page in range(100):
        data=get_json(base+'/jobs',dict(limit=20,offset=page*20,searchText=''))
        jobs=data['jobPostings']; total=max(total,data['total'])
        if not jobs and page*20 < total: raise ValueError('NVIDIA pagination ended early')
        for job in jobs:
            path=job['externalPath']
            if path in listing_seen: raise ValueError('NVIDIA repeated a page')
            listing_seen.add(path)
            if not ROLE.search(job['title']): continue
            if details>=250:
                result.report.update(partial=True,note='Reached 250 detail checks; remaining matches will require a wider scan.')
                return result
            if not path.startswith('/job/'): raise ValueError('Unexpected NVIDIA detail link')
            d=get_json(base+path)['jobPostingInfo']; details+=1
            if d.get('canApply') is False: continue
            locations=[d.get('location','')]+d.get('additionalLocations',[])
            locations=[loc for loc in locations if re.search(r'\bUS\b',loc) and re.search(r'\bRemote\b',loc,re.I)]
            result.add('nvidia',d['jobReqId'],d['title'],locations,
                'https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite'+path,d.get('startDate',''))
        if (page+1)*20>=total: break
    else: result.report.update(partial=True,note='Reached 2,000 listings; employer may have additional openings.')
    result.report['fetched']=len(listing_seen)
    return result


def amazon():
    result=Batch(); seen=set()
    for page in range(100):
        data=get_json('https://www.amazon.jobs/en/search.json?'+urlencode(dict(offset=page*100,result_limit=100,sort='recent',base_query='engineer',country='USA')))
        jobs=data['jobs']; total=data['hits']
        if data.get('error'): raise ValueError('Amazon search returned an error')
        if not jobs and page*100<total: raise ValueError('Amazon pagination ended early')
        for j in jobs:
            identity=str(j['id_icims'])
            if identity in seen: raise ValueError('Amazon repeated a page')
            seen.add(identity); locs=[]
            for entry in j.get('locations',[]):
                loc=json.loads(entry) if isinstance(entry,str) else entry
                label=loc.get('location','')
                if loc.get('countryIso2a')=='US' and (loc.get('type','').upper() in ('REMOTE','VIRTUAL') or re.search(r'\b(?:Virtual|Remote)\b',label,re.I)):
                    locs.append(label+' (remote)')
            path=j['job_path']
            if not path.startswith('/en/jobs/'): raise ValueError('Unexpected Amazon job link')
            date=j.get('posted_date','')
            try: date=datetime.strptime(date,'%B %d, %Y').date().isoformat()
            except ValueError: pass
            result.add('amazon',identity,j['title'],locs,'https://www.amazon.jobs'+path,date)
        if (page+1)*100>=total: break
    else: result.report.update(partial=True,note='Reached 10,000 engineering results; bounded coverage.')
    return result


def google():
    result=Batch(); seen=set()
    for page in range(1,21):
        raw=request('https://www.google.com/about/careers/applications/jobs/results/?'+urlencode(dict(has_remote='true',page=page)))
        match=re.search(r"key: 'ds:1'.*?data:",raw)
        if not match: raise ValueError('Google results format changed')
        data,_=json.JSONDecoder().raw_decode(raw[match.end():]); jobs=data[0] or []; total=data[2]
        if not isinstance(jobs,list) or not isinstance(total,int): raise ValueError('Invalid Google results')
        for j in jobs:
            if j[0] in seen: raise ValueError('Google repeated a page')
            seen.add(j[0]); locations=[]
            # Role-specific location statement separates remote eligibility from office locations.
            for value in walk(j):
                if isinstance(value,str):
                    m=re.search(r'Remote location\(s\):\s*([^<]+)',value)
                    if m:
                        locations.extend(loc.strip()+' (remote)' for loc in html.unescape(m[1]).split(';') if US.search(loc))
            result.add('google',j[0],j[1],locations,'https://www.google.com/about/careers/applications/jobs/results/'+j[0]+'/', '')
        if page*20>=total: break
        if not jobs: raise ValueError('Google pagination ended early')
    else: result.report.update(partial=True,note='Reached 400 remote-eligible listings.')
    return result


def apple():
    result=Batch(); seen=set(); page_signatures=set()
    for page in range(1,21):
        raw=request('https://jobs.apple.com/en-us/search?'+urlencode(dict(homeOffice='true',location='united-states-USA',page=page)))
        match=re.search(r'window.__staticRouterHydrationData = JSON.parse\(',raw)
        if not match: raise ValueError('Apple results format changed')
        encoded,_=json.JSONDecoder().raw_decode(raw[match.end():]); payload=json.loads(encoded)
        data=next((v for v in walk(payload) if isinstance(v,dict) and 'searchResults' in v),None)
        if data is None: raise ValueError('Apple results missing')
        jobs=data['searchResults']; total=data['totalRecords']
        signature=tuple(j['id'] for j in jobs)
        if signature in page_signatures: raise ValueError('Apple repeated a page')
        page_signatures.add(signature)
        for j in jobs:
            identity=j['positionId']
            seen.add(identity)
            locs=[loc.get('name','')+' (home office)' for loc in j['locations'] if loc.get('countryName') in ('United States','United States of America') and j.get('homeOffice') is True]
            result.add('apple',identity,j['postingTitle'],locs,
                'https://jobs.apple.com/en-us/details/'+identity+'/'+j['transformedPostingTitle'],j.get('postDateInGMT',''),True)
        if page*20>=total: break
        if not jobs: raise ValueError('Apple pagination ended early')
    else: result.report.update(partial=True,note='Reached 20 pages of home-office listings.')
    return result


def microsoft():
    result=Batch(); seen=set(); page_signatures=set(); remote='0 days / week in-office \u2013 remote'
    for page in range(100):
        data=get_json('https://apply.careers.microsoft.com/api/pcsx/search?'+urlencode(dict(domain='microsoft.com',query='',start=page*10,filter_work_site=remote)))['data']
        if remote not in data.get('appliedFilters',{}).get('workSite',[]): raise ValueError('Microsoft remote filter not applied')
        jobs=data['positions']; total=data['count']
        signature=tuple(j['id'] for j in jobs)
        if signature in page_signatures: raise ValueError('Microsoft repeated a page')
        page_signatures.add(signature)
        for j in jobs:
            identity=str(j['id'])
            seen.add(identity)
            locs=[loc+' (remote)' for loc in j['locations'] if US.search(loc)]
            path=j['positionUrl']
            if not path.startswith('/careers/job/'): raise ValueError('Unexpected Microsoft link')
            date=datetime.fromtimestamp(j['postedTs'],timezone.utc).isoformat() if j.get('postedTs') else ''
            result.add('microsoft',identity,j['name'],locs,'https://apply.careers.microsoft.com'+path,date,True)
        if (page+1)*10>=total: break
        if not jobs: raise ValueError('Microsoft pagination ended early')
    else: result.report.update(partial=True,note='Reached 1,000 remote listings.')
    return result


def netflix():
    result=Batch();raw=request('https://explore.jobs.netflix.net/careers')
    match=re.search(r'<code[^>]+id="smartApplyData"[^>]*>(.*?)</code>',raw,re.S)
    if not match: raise ValueError('Netflix public listings format changed')
    data=json.loads(html.unescape(match[1]))
    for j in data['positions']:
        locs=[loc for loc in j.get('locations',[]) if US.search(loc) and (j.get('work_location_option')=='remote' or re.search(r'\bRemote\b',loc,re.I))]
        url=j.get('canonicalPositionUrl','')
        if urlparse(url).hostname!='explore.jobs.netflix.net': raise ValueError('Unexpected Netflix link')
        result.add('netflix',str(j['id']),j['name'],locs,url)
    result.report.update(partial=True,note='Public first-page sample only. Search pagination/API returned HTTP 403; full coverage unavailable. Creation/update dates are not posting dates.')
    return result


def meta():
    raise ConnectorUnavailable('No verified public results parser yet; official career search requires further integration. Not a zero-jobs result.')


def fetch(company):
    if company not in {c['id'] for c in EMPLOYERS}: raise ValueError('Unknown employer')
    token=CURRENT_BATCH.set(None)
    deadline_token=SCAN_DEADLINE.set(time.monotonic()+300)
    try: return globals()[company]()
    except Exception as exc:
        batch=CURRENT_BATCH.get()
        if batch:
            code=getattr(exc,'code',None)
            if code==429:
                value=(getattr(exc,'headers',None) or {}).get('Retry-After','')
                try: delay=float(value)
                except (ValueError,TypeError):
                    try: delay=parsedate_to_datetime(value).timestamp()-time.time()
                    except (ValueError,TypeError): delay=3600
                batch.report['retry_seconds']=max(3600,delay)
            batch.report.update(partial=True,note='Scan incomplete: '+type(exc).__name__+(f' ({code})' if code else '')+'. Previously fetched matches retained.')
            return batch
        raise
    finally:
        CURRENT_BATCH.reset(token)
        SCAN_DEADLINE.reset(deadline_token)


def employer_id(job):
    name=job['company'].strip().casefold()
    aliases={'nvidia':'nvidia','nvidia corporation':'nvidia','amazon':'amazon','amazon web services':'amazon',
             'amazon.com services llc':'amazon','aws':'amazon','google':'google','apple':'apple',
             'microsoft':'microsoft','netflix':'netflix','meta':'meta','facebook':'meta'}
    return aliases.get(name)
