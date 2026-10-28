"""One background scanner per application process; SQLite connections stay thread-local."""
import json
import os
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from core import (Store, greenhouse, ashby, arbeitnow, weworkremotely, remoteok,
                  read_mail, load_board_directory, directory_batch)
from core import jobicy, arbeitnow_uk
from extra_sources import lever, himalayas, remotive, usajobs
from source_health import Health, refresh_directory
from request_limits import DeferredRequest
from curated import COMPANIES, monitor
from major_employers import EMPLOYERS, fetch as fetch_major, ConnectorUnavailable
import time
from board_repairs import load_audit, apply_audit, repaired_directory

DEFAULTS = dict(major_employers=[c['id'] for c in EMPLOYERS if c['id']!='meta'], curated=[c['id'] for c in COMPANIES if c['default']], minutes=5, boards=[], ashby_boards=[], wwr=True, batch_size=150,
                lever_boards=[], usajobs_email='', mail_host='', mail_user='', mail_folder='LinkedIn Jobs')

class Scanner:
    def __init__(self, data_dir, directory=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir/'jobs.sqlite3'
        self.settings_path = self.data_dir/'settings.json'
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.feed_wake = threading.Event()
        self.feed_thread = None
        self.stop = threading.Event()
        self.thread = None
        self.force_federal = False
        self.credential_path = self.data_dir/'usajobs-credential.json'
        self.usajobs_key = os.environ.get("USAJOBS_API_KEY", "")
        if self.credential_path.exists():
            self.usajobs_key = json.loads(self.credential_path.read_text(encoding='utf-8')).get('key','')
        self.mail = None
        self.settings = dict(DEFAULTS)
        if self.settings_path.exists():
            self.settings.update(json.loads(self.settings_path.read_text(encoding='utf-8')))
        self.seed_directory = load_board_directory(directory or Path(__file__).with_name('board-directory.json'))
        self.audit = load_audit()
        self.directory = repaired_directory(self.seed_directory,self.audit)
        db = Store(self.db_path)
        cursor = db.scan_cursor()
        saved = db.db.execute("SELECT data FROM source_diagnostics WHERE name='USAJOBS'").fetchone()
        self.federal_diagnostics = json.loads(saved[0]) if saved else {}
        db.db.close()
        self.state = dict(running=False, completed=0, total=0, errors=[], last_finished=None,
                          new_alerts=0, cursor=cursor, directory_size=len(self.directory),
                          successful=0, skipped=0, unavailable=[], recovered=[], repairs=[r for r in self.audit["boards"] if r["replacement"]], directory_notice='')

        self.feed_state = dict(running=False, completed=0, total=0, errors=[], last_finished=None,
                               new_alerts=0, successful=0, skipped=0)

    def snapshot(self):
        with self.lock:
            return {**self.state, 'errors':list(self.state['errors']) + list(self.feed_state['errors']),
                    'feeds':dict(self.feed_state), 'scan_mode':'continuous',
                    'new_alerts':self.state['new_alerts']+self.feed_state['new_alerts'], 'email_connected':bool(self.mail),
                    'usajobs_diagnostics':dict(self.federal_diagnostics),
                    'usajobs_configured':bool(self.usajobs_key and (self.settings.get('usajobs_email') or os.environ.get('USAJOBS_EMAIL'))),
                    'enabled': bool(self.thread and self.thread.is_alive())}

    def save_settings(self, settings, password='', disconnect=False, usajobs_key='', disconnect_usajobs=False):
        with self.lock:
            if disconnect_usajobs: self.usajobs_key = ""
            elif usajobs_key: self.usajobs_key = usajobs_key.strip()
            if disconnect_usajobs or usajobs_key:
                temp = self.credential_path.with_suffix('.tmp')
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump({'key':self.usajobs_key}, stream)
                temp.replace(self.credential_path)
            previous = self.settings
            self.settings = dict(settings)
            # Atomic replacement avoids a partially written settings file.
            temp = self.settings_path.with_suffix('.tmp')
            temp.write_text(json.dumps(settings, indent=2), encoding='utf-8')
            temp.replace(self.settings_path)
            if disconnect:
                self.mail = None
            elif password:
                self.mail = (settings['mail_host'],settings['mail_user'],password,settings['mail_folder'])
            elif any(previous.get(k) != settings.get(k) for k in ('mail_host','mail_user','mail_folder')):
                self.mail = None

    def request_usajobs_check(self):
        with self.lock:
            self.force_federal = True
        self.feed_wake.set()

    def record_federal(self, db, values):
        with self.lock:
            self.federal_diagnostics.update(values)
            with db.db:
                db.db.execute('INSERT OR REPLACE INTO source_diagnostics VALUES (?,?)',
                    ('USAJOBS',json.dumps(self.federal_diagnostics)))

    def start(self):
        if self.thread: return
        self.thread = threading.Thread(target=self.loop, args=('directory',), name='jobradar-directory', daemon=True)
        self.feed_thread = threading.Thread(target=self.loop, args=('feeds',), name='jobradar-feeds', daemon=True)
        self.thread.start()
        self.feed_thread.start()

    def trigger(self):
        self.wake.set()
        self.feed_wake.set()
        return True

    def loop(self, lane='directory'):
        wake = self.feed_wake if lane == 'feeds' else self.wake
        progress = self.feed_state if lane == 'feeds' else self.state
        while not self.stop.is_set():
            wake.clear()
            failed = False
            try:
                self.run_once(lane)
            except Exception as exc:
                failed = True
                logging.error('Scan failed: %s',type(exc).__name__)
                with self.lock:
                    progress.update(running=False, errors=[f'Scan failed: {type(exc).__name__}'])
            with self.lock:
                seconds = max(5, int(self.settings['minutes'])) * 60 if lane == 'feeds' else 5
            wake.wait(60 if failed else seconds)

    def run_once(self, lane='all'):
        progress = self.feed_state if lane == 'feeds' else self.state
        started = time.monotonic()
        with self.lock:
            settings = dict(self.settings)
            mail = self.mail
            federal_key = self.usajobs_key
            federal_email = settings.get("usajobs_email") or os.environ.get("USAJOBS_EMAIL", "")
            if progress['running']: return
            force_federal = self.force_federal if lane != 'directory' else False
            if lane != 'directory': self.force_federal = False
            progress.update(running=True, completed=0, errors=[], new_alerts=0, successful=0, skipped=0, recovered=[])
        db = Store(self.db_path)
        try:
            health = Health(db.db)
            if lane != 'feeds':
                apply_audit(health,self.audit)
                self.directory, notice, updated = refresh_directory(self.seed_directory,self.data_dir)
                self.directory = repaired_directory(self.directory,self.audit)
                with self.lock:
                    progress.update(directory_notice=notice,directory_updated=updated,directory_size=len(self.directory))
            batch, cursor = directory_batch(self.directory, db.scan_cursor(), settings['batch_size']) if lane != 'feeds' else ([],db.scan_cursor())
            tasks = {'Arbeitnow':arbeitnow, 'Arbeitnow UK':arbeitnow_uk, 'Remote OK':remoteok, 'Jobicy':jobicy, 'Himalayas':himalayas, 'Remotive':remotive}
            if lane == 'feeds':
                for company in settings.get('major_employers',[]):
                    if company in {c['id'] for c in EMPLOYERS}:
                        tasks['Major: '+company] = lambda c=company:fetch_major(c)
            federal_stats = {}
            if federal_key and federal_email: tasks['USAJOBS'] = lambda:usajobs(federal_email,federal_key,federal_stats)
            if settings['wwr']: tasks['We Work Remotely'] = weworkremotely
            for b in batch:
                fn = {'ashby':ashby,'greenhouse':greenhouse,'lever':lever}[b['vendor']]
                name = ({'ashby':'Ashby: ','greenhouse':'Greenhouse: ','lever':'Lever: '}[b['vendor']]) + b['slug']
                tasks[name] = lambda f=fn, slug=b['slug']: f(slug)
            for key, prefix, fn in [('boards','Greenhouse: ',greenhouse),('ashby_boards','Ashby: ',ashby),('lever_boards','Lever: ',lever)]:
                for slug in settings[key]: tasks[prefix+slug] = lambda f=fn,b=slug:f(b)
            for repair in self.audit['boards']:
                if repair['replacement']: tasks.pop(repair['name'],None)
            if mail: tasks['LinkedIn email'] = lambda:read_mail(mail)
            # Successfully resolved alternate addresses receive frequent checks.
            for row in health.report():
                name = row['replacement']
                if name:
                    provider,slug = name.split(': ',1)
                    fn = {'Ashby':ashby,'Greenhouse':greenhouse,'Lever':lever}[provider]
                    tasks[name] = lambda f=fn,b=slug:f(b)
            curated_tasks = {}
            for company in COMPANIES:
                if company['id'] not in settings.get('curated',[]): continue
                name = ('Greenhouse: ' if company['kind']=='api' else 'Careers: ') + company['id']
                curated_tasks[name] = company['id']
                tasks[name] = lambda c=company: monitor(c)
            def is_board(name): return name.startswith(('Ashby: ','Greenhouse: ','Lever: ')) and name not in curated_tasks
            if lane == 'directory': tasks = {name:fn for name,fn in tasks.items() if is_board(name)}
            if lane == 'feeds': tasks = {name:fn for name,fn in tasks.items() if not is_board(name)}
            due = {name:fn for name,fn in tasks.items() if health.due(name) or (name == 'USAJOBS' and force_federal)}
            with self.lock: progress['skipped'] = len(tasks)-len(due)
            tasks = due
            with self.lock: progress['total'] = len(tasks)
            for name in tasks:
                if name.startswith('Major: '):
                    saved=db.db.execute('SELECT data FROM source_diagnostics WHERE name=?',(name,)).fetchone()
                    report=json.loads(saved[0]) if saved else {}
                    report.update(status='scanning',last_attempt=datetime.now(timezone.utc).isoformat())
                    with db.db: db.db.execute('INSERT OR REPLACE INTO source_diagnostics VALUES (?,?)',(name,json.dumps(report)))
            with ThreadPoolExecutor(max_workers=4 if lane == 'feeds' else 12) as pool:
                futures = {pool.submit(fn):name for name,fn in tasks.items()}
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        rows = future.result()
                        new = db.ingest(name, rows)
                        if name.startswith('Major: '):
                            report=dict(rows.report, status='partial' if rows.report['partial'] else 'ok',
                                        last_attempt=datetime.now(timezone.utc).isoformat(), last_success=datetime.now(timezone.utc).isoformat())
                            with db.db: db.db.execute('INSERT OR REPLACE INTO source_diagnostics VALUES (?,?)',(name,json.dumps(report)))
                        if name == 'USAJOBS':
                            self.record_federal(db, dict(status='ok', error='', last_attempt=datetime.now(timezone.utc).isoformat(),
                                last_success=datetime.now(timezone.utc).isoformat(), counts=federal_stats))
                        if name in curated_tasks:
                            with db.db:
                                db.db.execute('INSERT OR REPLACE INTO curated_checks VALUES (?,?,?,?,?)',
                                    (curated_tasks[name],time.time(),'ok',len(rows),''))
                        health.record(name,'ok',max(3600,getattr(rows,'report',{}).get('retry_seconds',0)) if name in curated_tasks or name.startswith('Major: ') else {'Jobicy':3600,'Arbeitnow UK':900,'Himalayas':86400,'Remotive':21600,'USAJOBS':3600}.get(name,300))
                        with self.lock:
                            progress['new_alerts'] += len(new)
                            progress['successful'] += 1
                    except DeferredRequest as exc:
                        health.record(name,'cooldown',exc.seconds)
                        with self.lock: progress['skipped'] += 1
                    except Exception as exc:
                        code = getattr(exc, 'code', None)
                        error = f'{name}: {type(exc).__name__}' + (f' ({code})' if code else '')
                        if name.startswith('Major: '):
                            saved=db.db.execute('SELECT data FROM source_diagnostics WHERE name=?',(name,)).fetchone()
                            report=json.loads(saved[0]) if saved else {}
                            report.update(status='unsupported' if isinstance(exc,ConnectorUnavailable) else 'error',
                                last_attempt=datetime.now(timezone.utc).isoformat(),note=str(exc) if isinstance(exc,ConnectorUnavailable) else error)
                            with db.db: db.db.execute('INSERT OR REPLACE INTO source_diagnostics VALUES (?,?)',(name,json.dumps(report)))
                        if name == 'USAJOBS':
                            message = ('Authentication rejected; check the registered email and API key.' if code in (401,403)
                                else 'Rate limited; retry will follow the source cooldown.' if code == 429 else error)
                            self.record_federal(db, dict(status='error', error=message, last_attempt=datetime.now(timezone.utc).isoformat()))
                        if name in curated_tasks:
                            with db.db:
                                db.db.execute('INSERT OR REPLACE INTO curated_checks VALUES (?,?,?,?,?)',
                                    (curated_tasks[name],time.time(),'error',0,error))
                        status = health.failure(name,exc,board=name.startswith(('Ashby: ','Greenhouse: ','Lever: ')))
                        if status!='unavailable':
                            with self.lock: progress['errors'].append(error)
                    finally:
                        with self.lock: progress['completed'] += 1
            if lane != 'feeds': db.save_scan_cursor(cursor)
            with self.lock:
                progress.update(last_finished=datetime.now(timezone.utc).isoformat())
                if lane != 'feeds':
                    progress.update(cursor=cursor, unavailable=[r for r in health.report() if r['status']=='unavailable'])
        finally:
            db.db.close()
            with self.lock:
                progress['running'] = False
                progress['duration_seconds'] = round(time.monotonic()-started,1)
