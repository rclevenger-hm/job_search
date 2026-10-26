"""Durable scheduling, quarantine, and validated public directory refresh."""
import json
import re
import time
import urllib.request
from pathlib import Path

DIRECTORY_URL = 'https://raw.githubusercontent.com/moonie0201/ats-directory/main/companies.jsonl'

class Health:
    def __init__(self, db):
        self.db = db
        self.db.execute('''CREATE TABLE IF NOT EXISTS source_health (
            name TEXT PRIMARY KEY, status TEXT, failures INTEGER, retry_at REAL,
            detail TEXT, replacement TEXT DEFAULT '')''')
        self.db.commit()

    def get(self, name):
        row = self.db.execute('SELECT status,failures,retry_at,detail,replacement FROM source_health WHERE name=?',(name,)).fetchone()
        return dict(zip(('status','failures','retry_at','detail','replacement'),row)) if row else None

    def due(self, name, now=None):
        row = self.get(name)
        return not row or row['retry_at'] <= (time.time() if now is None else now)

    def record(self,name,status,delay,detail='',replacement='',now=None):
        previous = self.get(name)
        failures = (previous['failures'] if previous else 0)+1 if status!='ok' else 0
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?)',
                (name,status,failures,(time.time() if now is None else now)+delay,detail,replacement))

    def failure(self,name,exc,board=False,now=None):
        code = getattr(exc,'code',None)
        if board and code in (404,410):
            status,delay = 'unavailable',30*86400
        else:
            row = self.get(name)
            delay = min(86400,900 * 2**min(6,row['failures'] if row else 0))
            status = 'error'
            if code==429:
                value = (getattr(exc,'headers',None) or {}).get('Retry-After','')
                try: delay = max(delay,int(value))
                except (ValueError,TypeError):
                    from email.utils import parsedate_to_datetime
                    try: delay = max(delay,parsedate_to_datetime(value).timestamp()-(now or time.time()))
                    except (ValueError,TypeError): pass
        delay=max(delay, {'Jobicy':3600,'Himalayas':86400,'Remotive':21600,'USAJOBS':3600}.get(name,0))
        self.record(name,status,delay,f'{type(exc).__name__}'+(f' ({code})' if code else ''),now=now)
        return status

    def report(self):
        rows = self.db.execute('SELECT name,status,retry_at,detail,replacement FROM source_health WHERE status != ? ORDER BY name',('ok',)).fetchall()
        return [dict(zip(('name','status','retry_at','detail','replacement'),r)) for r in rows]

def refresh_directory(seed, data_dir, now=None):
    """Merge newly published addresses; never trust registry URLs or execute its content."""
    now = time.time() if now is None else now
    path = Path(data_dir)/'directory-cache.json'
    try: cache = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError,ValueError): cache = {}
    notice = ''
    if cache.get('version') != 2 or now-cache.get('checked_at',0) >= 86400:
        try:
            request = urllib.request.Request(DIRECTORY_URL,headers={'User-Agent':'JobRadar/2.0'})
            with urllib.request.urlopen(request,timeout=20) as response:
                raw = response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024: raise ValueError('Directory too large')
            additions = []
            for line in raw.decode('utf-8').splitlines():
                row = json.loads(line)
                if row.get('status')!='ok' or row.get('provider') not in ('greenhouse','ashby','lever'): continue
                slug = row.get('slug','')
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',slug): continue
                additions.append(dict(vendor=row['provider'],slug=slug,company=row.get('name') or slug))
            if not additions: raise ValueError('Empty directory')
            cache = dict(version=2,checked_at=now,updated_at=now,boards=additions)
        except Exception as exc:
            notice = 'Directory refresh failed: '+type(exc).__name__+'; keeping cached boards.'
            cache['checked_at'] = now
            cache['version'] = 2
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(cache),encoding='utf-8')
        temp.replace(path)
    merged = {(b['vendor'],b['slug']):b for b in seed}
    for b in cache.get('boards',[]): merged.setdefault((b['vendor'],b['slug']),b)
    return list(merged.values()),notice,cache.get('updated_at')
