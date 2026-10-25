"""Additional public feeds. Unverified publication times never enter fresh alerts."""
import json
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
from core import ROLE, US, eligibility, fetch_json

QUERIES = ('site reliability', 'devops', 'platform engineer')

def row(key, title, company, location, url, source, posted='', source_date='', expires=0):
    return dict(key=key, title=title, company=company, location=location, url=url,
                source=source, posted=posted, source_date=source_date, expires=expires,
                eligibility=eligibility(location, True))

def unix_date(value):
    try: return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError): return ''

def lever(board):
    results = []
    for page in range(20):
        jobs = fetch_json('https://api.lever.co/v0/postings/' + quote(board,safe='') +
                          '?' + urlencode(dict(mode='json',skip=page*100,limit=100)))
        for j in jobs:
            if not ROLE.search(j.get('text','')) or j.get('workplaceType') != 'remote': continue
            country = j.get('country') or ''
            if country and country.upper() != 'US': continue
            loc = ('United States; ' if country.upper() == 'US' else '') + j.get('categories',{}).get('location','')
            created = unix_date(j['createdAt']/1000) if isinstance(j.get('createdAt'),(int,float)) else ''
            results.append(row('lever:'+board+':'+j['id'],j['text'],board,loc,j['hostedUrl'],
                               'Lever (browse only)',source_date='Created: '+created if created else ''))
        if len(jobs)<100: break
    return results

def remotive():
    results = []
    for j in fetch_json('https://remotive.com/api/remote-jobs')['jobs']:
        if not ROLE.search(j.get('title','')): continue
        loc = j.get('candidate_required_location','')
        if loc and not US.search(loc): continue
        results.append(row('remotive:'+str(j['id']),j['title'],j.get('company_name',''),
            loc,j['url'],'Remotive (24-hour delay; browse only)',
            source_date='Published (timezone unverified): '+j.get('publication_date',''),
            expires=time.time()+21600))
    return results

def himalayas():
    results = {}
    for query in QUERIES:
        for page in range(1,6):
            data = fetch_json('https://himalayas.app/jobs/api/search?' + urlencode(
                dict(q=query,country='US',sort='recent',page=page)))
            jobs = data['jobs']
            for j in jobs:
                if not ROLE.search(j.get('title','')): continue
                countries = j.get('locationRestrictions') or []
                names = [c.get('name',c.get('alpha2','')) if isinstance(c,dict) else c for c in countries]
                if names and not any(c in ('US','United States','United States of America') for c in names): continue
                expiry = j.get('expiryDate')
                # Hide records after one daily refresh window, including disappeared listings.
                expires = min(float(expiry),time.time()+86400) if expiry else time.time()+86400
                if expires <= time.time(): continue
                key = 'himalayas:'+j['guid']
                results[key] = row(key,j['title'],j.get('companyName',''),'; '.join(names) or 'Worldwide',
                    j['applicationLink'],'Himalayas (daily; browse only)',
                    source_date='Last updated: '+unix_date(j.get('pubDate')),expires=expires)
            if len(jobs)<20: break
    return list(results.values())

def usajobs(email, api_key):
    results = {}
    for query in QUERIES:
        for page in range(1,6):
            url = 'https://data.usajobs.gov/api/Search?' + urlencode(dict(
                Keyword=query,RemoteIndicator='True',ResultsPerPage=100,Page=page))
            request = urllib.request.Request(url,headers={'User-Agent':email,
                'Authorization-Key':api_key,'Host':'data.usajobs.gov','Accept':'application/json'})
            with urllib.request.urlopen(request,timeout=20) as response: data=json.load(response)
            items=data['SearchResult']['SearchResultItems']
            for item in items:
                j=item['MatchedObjectDescriptor']
                if not ROLE.search(j.get('PositionTitle','')): continue
                stamp=j.get('PublicationStartDate','')
                # A date or timestamp without timezone cannot prove freshness.
                try:
                    parsed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                    posted=stamp if parsed.tzinfo else ''
                except ValueError: posted=''
                key='usajobs:'+str(item['MatchedObjectId'])
                results[key]=row(key,j['PositionTitle'],j.get('OrganizationName',''),
                    'US Remote; '+j.get('PositionLocationDisplay',''),j['PositionURI'],'USAJOBS',
                    posted=posted,source_date='Publication date: '+stamp,expires=time.time()+3600)
            if len(items)<100: break
    return list(results.values())
