"""JobRadar: public feeds, read-only mail ingestion, filtering and persistence."""
import email
import html
import imaplib
import json
import re
import sqlite3
import ssl
import uuid
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from email import policy
from html.parser import HTMLParser
from urllib.parse import urlparse, quote
from pathlib import Path

ROLE = re.compile(r"site[ -]reliability|\bsre\b|dev[ -]?ops|platform\s+(?:software\s+)?engineer|engineer[^\n]{0,35}\bplatform\b", re.I)
US = re.compile(r"\b(?:usa|us|u\.s\.|united states|north america|americas|worldwide|anywhere|global)\b", re.I)
REMOTE = re.compile(r"\bremote\b|work from home|worldwide|anywhere", re.I)
NOT_REMOTE = re.compile(r"hybrid|on[ -]?site|not remote|no remote", re.I)

def posted_within_24_hours(value, now=None):
    """Require a verified timestamp with an age strictly below 24 hours."""
    if not value:
        return False
    try:
        posted = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if posted.tzinfo is None:
            return False
        age = (now or datetime.now(timezone.utc)) - posted
        return timedelta(0) <= age < timedelta(hours=24)
    except (ValueError, TypeError):
        return False

def posted_age(value, now=None):
    if not value:
        return 'Unknown'
    try:
        posted = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if posted.tzinfo is None:
            return 'Unknown'
        seconds = int(((now or datetime.now(timezone.utc)) - posted).total_seconds())
    except (ValueError, TypeError):
        return 'Unknown'
    if seconds < 0: return 'Future date'
    if seconds < 60: return 'Just posted'
    if seconds < 3600: return f'{seconds // 60}m ago'
    if seconds < 86400: return f'{seconds // 3600}h ago'
    return f'{seconds // 86400}d ago'

def eligibility(location, remote=None):
    if NOT_REMOTE.search(location) or remote is False:
        return 'Excluded'
    if not (remote is True or REMOTE.search(location)):
        return 'Review'
    if US.search(location):
        return 'Ready'
    if re.search(r'\b(?:europe|emea|apac|india|germany|uk|united kingdom|canada|australia|poland|brazil|latam)\b', location, re.I):
        return 'Excluded'
    return 'Review'

def safe_url(url):
    p = urlparse(url)
    return p.scheme == 'https' and bool(p.hostname) and not p.username

def linkedin_url(url):
    p = urlparse(html.unescape(url))
    if p.scheme != 'https' or not (p.hostname == 'linkedin.com' or (p.hostname or '').endswith('.linkedin.com')):
        return None
    m = re.search(r'/jobs/view/(?:[^/?]*-)?(\d+)(?:/|$)', p.path)
    return 'https://www.linkedin.com/jobs/view/' + m.group(1) if m else None

def fetch_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'JobRadar/1.0 personal job alerts', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)

def greenhouse(board):
    data = fetch_json('https://boards-api.greenhouse.io/v1/boards/' + quote(board, safe='') + '/jobs')
    result = []
    for j in data['jobs']:
        if not ROLE.search(j['title']):
            continue
        loc = j.get('location', {}).get('name', '')
        result.append(dict(key=f"gh:{board}:{j['id']}", title=j['title'], company=board,
                           location=loc, url=j['absolute_url'], source='Greenhouse', eligibility=eligibility(loc),
                           posted=j.get('first_published') or ''))
    return result

def ashby(board):
    result = []
    for j in fetch_json('https://api.ashbyhq.com/posting-api/job-board/' + quote(board, safe=''))['jobs']:
        if not j.get('isListed', False) or not ROLE.search(j.get('title', '')):
            continue
        # isRemote includes hybrid in Ashby; require its explicit workplace type.
        if j.get('workplaceType') != 'Remote':
            continue
        locations = [j.get('location', '')]
        addresses = [j.get('address', {})]
        for secondary in j.get('secondaryLocations') or []:
            locations.append(secondary.get('location', ''))
            addresses.append(secondary.get('address', {}))
        for address in addresses:
            address = address or {}
            postal = address.get('postalAddress') or address
            locations.append(postal.get('addressCountry') or '')
        loc = '; '.join(dict.fromkeys(x for x in locations if x))
        url = j.get('jobUrl', '')
        if not safe_url(url): continue
        result.append(dict(key='ashby:' + board + ':' + (j.get('id') or url), title=j['title'],
            company=board, location=loc, url=url, source='Ashby (last published)',
            eligibility=eligibility(loc, True), posted=j.get('publishedAt') or ''))
    return result

def parse_wwr(data):
    result = []
    for item in ET.fromstring(data).findall('./channel/item'):
        label = html.unescape(item.findtext('title', ''))
        company, separator, title = label.partition(': ')
        if not separator: title, company = label, 'Not supplied'
        if not ROLE.search(title): continue
        url = item.findtext('link', '').strip()
        if not safe_url(url) or urlparse(url).hostname not in ('weworkremotely.com','www.weworkremotely.com'):
            continue
        loc = '; '.join(item.findtext(k, '').strip() for k in ('region','country','state') if item.findtext(k, '').strip())
        try:
            posted = parsedate_to_datetime(item.findtext('pubDate', '')).isoformat()
        except (TypeError, ValueError, OverflowError):
            posted = ''
        result.append(dict(key='wwr:' + url, title=title, company=company, location=loc or 'Remote — verify country',
            url=url, source='We Work Remotely', eligibility=eligibility(loc, True), posted=posted))
    return result

def weworkremotely():
    url = 'https://weworkremotely.com/remote-jobs.rss'
    request = urllib.request.Request(url, headers={'User-Agent':'JobRadar/1.0 personal job alerts', 'Accept':'application/rss+xml'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return parse_wwr(response.read())

def remoteok():
    result = []
    for j in fetch_json('https://remoteok.com/api'):
        if not ROLE.search(j.get('position', '')): continue
        url = j.get('url', '')
        if not safe_url(url) or urlparse(url).hostname not in ('remoteok.com', 'www.remoteok.com'): continue
        loc = j.get('location') or ''
        result.append(dict(key='remoteok:' + str(j['id']), title=j['position'],
            company=j.get('company', ''), location=loc or 'Remote — verify country', url=url,
            source='Remote OK', eligibility=eligibility(loc, True), posted=j.get('date') or ''))
    return result

def load_board_directory(path):
    rows = json.loads(Path(path).read_text(encoding='utf-8'))
    unique = {}
    for row in rows:
        vendor, slug = row.get('vendor'), row.get('slug', '')
        if vendor in ('greenhouse', 'ashby', 'lever') and re.fullmatch(r'[A-Za-z0-9_-]+', slug):
            unique[(vendor, slug)] = dict(vendor=vendor, slug=slug, company=row.get('company', slug))
    return list(unique.values())

def directory_batch(boards, cursor, size=150):
    if not boards: return [], 0
    start = cursor % len(boards)
    end = min(start + size, len(boards))
    return boards[start:end], 0 if end == len(boards) else end

def jobicy():
    result, cursor = [], None
    for _ in range(20):
        url = 'https://jobicy.com/api/v2/remote-jobs?count=100'
        if cursor: url += '&cursor=' + quote(cursor, safe='')
        data = fetch_json(url)
        jobs = data['jobs']
        for j in jobs:
            title = html.unescape(j.get('jobTitle', ''))
            if not ROLE.search(title): continue
            loc = j.get('jobGeo') or ''
            url = j.get('url', '')
            if urlparse(url).hostname != 'jobicy.com': continue
            result.append(dict(key='jobicy:'+str(j['id']), title=title, company=j.get('companyName',''),
                location=loc, url=url, source='Jobicy', eligibility=(eligibility(loc,True) if not loc or US.search(loc) else 'Excluded'), posted=j.get('pubDate') or ''))
        cursor = data.get('nextCursor')
        if not cursor: break
        # Documented order: publication time descending. Stop after passing the cutoff.
        stamp = jobs[-1].get('pubDate','') if jobs else ''
        try:
            if datetime.now(timezone.utc)-datetime.fromisoformat(stamp) >= timedelta(days=1): break
        except (ValueError,TypeError): pass
    return result

def arbeitnow_uk():
    return arbeitnow('https://www.arbeitnow.co.uk/api/job-board-api', 'Arbeitnow UK')

def arbeitnow(endpoint='https://www.arbeitnow.com/api/job-board-api', source='Arbeitnow'):
    result = []
    # The public first page contains the latest listings; no full historical crawl.
    for j in fetch_json(endpoint)['data']:
        if not ROLE.search(j['title']):
            continue
        loc = j.get('location', '')
        stamp = j.get('created_at')
        posted = datetime.fromtimestamp(stamp, timezone.utc).isoformat() if isinstance(stamp, (int, float)) else ''
        result.append(dict(key=('arbeitnow:' if source=='Arbeitnow' else 'arbeitnow-uk:') + j['slug'], title=j['title'], company=j.get('company_name', ''),
                           location=loc, url=j['url'], source=source, eligibility=eligibility(loc, j.get('remote')), posted=posted))
    return result

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.active = None
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.active = [dict(attrs).get('href', ''), '']
    def handle_data(self, data):
        if self.active is not None:
            self.active[1] += data + ' '
    def handle_endtag(self, tag):
        if tag == 'a' and self.active is not None:
            self.links.append(self.active)
            self.active = None

def parse_email(raw):
    message = email.message_from_bytes(raw, policy=policy.default)
    result = {}
    for part in message.walk():
        if part.get_content_type() != 'text/html' or part.get_content_disposition() == 'attachment':
            continue
        parser = Links()
        parser.feed(part.get_content())
        for href, text in parser.links:
            url = linkedin_url(href)
            title = ' '.join(text.split())
            if url and ROLE.search(title):
                # Email layouts vary. Never borrow remote/location claims from another card.
                result[url] = dict(key='linkedin:' + url.rsplit('/', 1)[-1], title=title,
                    company='LinkedIn alert', location='Verify US eligibility and remote status',
                    url=url, source='LinkedIn email', eligibility='Review', posted='')
    return list(result.values())

def read_mail(settings):
    host, username, password, folder = settings
    jobs = []
    # Read-only selection and BODY.PEEK avoid marking messages as read.
    with imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=20) as mail:
        mail.login(username, password)
        status, _ = mail.select('"' + folder.replace('"', '').replace('\\', '') + '"', readonly=True)
        if status != 'OK':
            raise ValueError('Mailbox folder could not be opened')
        since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime('%d-%b-%Y')
        status, data = mail.uid('search', None, 'SINCE', since, 'FROM', '"linkedin.com"')
        if status != 'OK':
            raise ValueError('Mailbox search failed')
        for uid in data[0].split()[-50:]:
            status, parts = mail.uid('fetch', uid, '(BODY.PEEK[])')
            if status != 'OK':
                raise ValueError('Mailbox message fetch failed')
            for part in parts:
                if isinstance(part, tuple):
                    jobs.extend(parse_email(part[1]))
    return jobs

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            key TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, url TEXT,
            source TEXT, eligibility TEXT, posted TEXT, discovered TEXT, status TEXT DEFAULT 'New')''')
        self.db.execute('CREATE TABLE IF NOT EXISTS job_metadata (key TEXT PRIMARY KEY, source_date TEXT, expires REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS curated_checks (company TEXT PRIMARY KEY, checked REAL, status TEXT, matches INTEGER, error TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS sources (name TEXT PRIMARY KEY)')
        self.db.execute('CREATE TABLE IF NOT EXISTS scan_state (name TEXT PRIMARY KEY, cursor INTEGER NOT NULL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS applications (
            job_key TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL,
            url TEXT, applied_on TEXT, stage TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '')''')
        # Preserve old applied flags without inventing historical application dates.
        self.db.execute('''INSERT OR IGNORE INTO applications
            SELECT key, title, company, url, '', 'Applied', '' FROM jobs WHERE status='Applied' ''')
        self.db.execute('CREATE TABLE IF NOT EXISTS source_diagnostics (name TEXT PRIMARY KEY, data TEXT NOT NULL)')
        if not self.db.execute("SELECT 1 FROM scan_state WHERE name='usajobs_deadline_v1'").fetchone():
            self.db.execute("UPDATE job_metadata SET expires=0 WHERE key IN (SELECT key FROM jobs WHERE source='USAJOBS')")
            self.db.execute("INSERT INTO scan_state VALUES ('usajobs_deadline_v1',1)")
        self.db.commit()

    def ingest(self, source, jobs):
        baseline = self.db.execute('SELECT 1 FROM sources WHERE name=?', (source,)).fetchone() is None
        new = []
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with self.db:
            for job in jobs:
                if not safe_url(job['url']) or job['eligibility'] == 'Excluded':
                    continue
                values = [job[k] for k in ('key','title','company','location','url','source','eligibility','posted')]
                inserted = self.db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (*values, now, 'Existing' if baseline else 'New')).rowcount
                if not inserted and job.get('posted'):
                    self.db.execute('UPDATE jobs SET posted=? WHERE key=?', (job['posted'], job['key']))
                if 'source_date' in job or 'expires' in job:
                    self.db.execute('INSERT OR REPLACE INTO job_metadata VALUES (?,?,?)',
                        (job['key'],job.get('source_date',''),job.get('expires',0)))
                if inserted and not baseline and posted_within_24_hours(job.get('posted')):
                    new.append(job)
            self.db.execute('INSERT OR IGNORE INTO sources VALUES (?)', (source,))
        return new

    def rows(self):
        self.db.row_factory = sqlite3.Row
        return [dict(row) for row in self.db.execute("""SELECT jobs.*, COALESCE(m.source_date, '') AS source_date FROM jobs LEFT JOIN job_metadata m ON jobs.key=m.key WHERE COALESCE(m.expires,0)=0 OR m.expires>unixepoch() ORDER BY discovered DESC, title""")]

    def scan_cursor(self):
        row = self.db.execute("SELECT cursor FROM scan_state WHERE name='directory'").fetchone()
        return row[0] if row else 0

    def save_scan_cursor(self, cursor):
        with self.db:
            self.db.execute("INSERT INTO scan_state VALUES ('directory',?) ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor", (cursor,))

    def mark(self, key, status):
        with self.db:
            self.db.execute('UPDATE jobs SET status=? WHERE key=?', (status, key))
            if status == 'Applied':
                self.db.execute('''INSERT OR IGNORE INTO applications
                    SELECT key,title,company,url,?,'Applied','' FROM jobs WHERE key=?''',
                    (datetime.now().astimezone().date().isoformat(), key))

    def applications(self):
        self.db.row_factory = sqlite3.Row
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM applications ORDER BY applied_on DESC, company, title')]

    def save_application(self, job_key, title, company, url, applied_on, stage, notes):
        title, company, url, applied_on = (v.strip() for v in (title, company, url, applied_on))
        if not title or not company:
            raise ValueError('Enter a job title and company.')
        if url and not safe_url(url):
            raise ValueError('Use an HTTPS application link, or leave it blank.')
        if applied_on:
            try:
                parsed = datetime.strptime(applied_on, '%Y-%m-%d').date()
                if parsed.isoformat() != applied_on: raise ValueError()
            except ValueError:
                raise ValueError('Use YYYY-MM-DD for the application date, or leave it blank if unknown.')
        if stage not in ('Applied', 'Interviewing', 'Offer', 'Rejected', 'Withdrawn'):
            raise ValueError('Choose an application stage.')
        key = job_key or 'manual:' + uuid.uuid4().hex
        with self.db:
            self.db.execute('''INSERT INTO applications VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(job_key) DO UPDATE SET title=excluded.title, company=excluded.company,
                url=excluded.url, applied_on=excluded.applied_on, stage=excluded.stage, notes=excluded.notes''',
                (key,title,company,url,applied_on,stage,notes))
            self.db.execute("UPDATE jobs SET status='Applied' WHERE key=?", (key,))
        return key
