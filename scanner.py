"""One background scanner per application process; SQLite connections stay thread-local."""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from core import (Store, greenhouse, ashby, arbeitnow, weworkremotely, remoteok,
                  read_mail, load_board_directory, directory_batch)
from core import jobicy, arbeitnow_uk
from source_health import Health, refresh_directory

DEFAULTS = dict(minutes=5, boards=[], ashby_boards=[], wwr=True, batch_size=150,
                mail_host='', mail_user='', mail_folder='LinkedIn Jobs')

class Scanner:
    def __init__(self, data_dir, directory=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir/'jobs.sqlite3'
        self.settings_path = self.data_dir/'settings.json'
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.mail = None
        self.settings = dict(DEFAULTS)
        if self.settings_path.exists():
            self.settings.update(json.loads(self.settings_path.read_text(encoding='utf-8')))
        self.seed_directory = load_board_directory(directory or Path(__file__).with_name('board-directory.json'))
        self.directory = list(self.seed_directory)
        db = Store(self.db_path)
        cursor = db.scan_cursor()
        db.db.close()
        self.state = dict(running=False, completed=0, total=0, errors=[], last_finished=None,
                          new_alerts=0, cursor=cursor, directory_size=len(self.directory),
                          successful=0, skipped=0, unavailable=[], recovered=[], directory_notice='')

    def snapshot(self):
        with self.lock:
            return {**self.state, 'errors':list(self.state['errors']), 'email_connected':bool(self.mail),
                    'enabled': bool(self.thread and self.thread.is_alive())}

    def save_settings(self, settings, password='', disconnect=False):
        with self.lock:
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

    def start(self):
        if self.thread: return
        self.thread = threading.Thread(target=self.loop, name='jobradar-scanner', daemon=True)
        self.thread.start()

    def trigger(self):
        if self.snapshot()['running']: return False
        self.wake.set()
        return True

    def loop(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                self.run_once()
            except Exception as exc:
                logging.error('Scan failed: %s',type(exc).__name__)
                with self.lock:
                    self.state.update(running=False, errors=[f'Scan failed: {type(exc).__name__}'])
            with self.lock:
                seconds = max(5, int(self.settings['minutes'])) * 60
            self.wake.wait(seconds)

    def run_once(self):
        with self.lock:
            settings = dict(self.settings)
            mail = self.mail
            if self.state['running']: return
            self.state.update(running=True, completed=0, errors=[], new_alerts=0, successful=0, skipped=0, recovered=[])
        db = Store(self.db_path)
        try:
            health = Health(db.db)
            self.directory, notice, updated = refresh_directory(self.seed_directory,self.data_dir)
            with self.lock:
                self.state.update(directory_notice=notice,directory_updated=updated,directory_size=len(self.directory))
            batch, cursor = directory_batch(self.directory, db.scan_cursor(), settings['batch_size'])
            tasks = {'Arbeitnow':arbeitnow, 'Arbeitnow UK':arbeitnow_uk, 'Remote OK':remoteok, 'Jobicy':jobicy}
            if settings['wwr']: tasks['We Work Remotely'] = weworkremotely
            for b in batch:
                fn = ashby if b['vendor']=='ashby' else greenhouse
                name = ('Ashby: ' if b['vendor']=='ashby' else 'Greenhouse: ') + b['slug']
                tasks[name] = lambda f=fn, slug=b['slug']: f(slug)
            for key, prefix, fn in [('boards','Greenhouse: ',greenhouse),('ashby_boards','Ashby: ',ashby)]:
                for slug in settings[key]: tasks[prefix+slug] = lambda f=fn,b=slug:f(b)
            if mail: tasks['LinkedIn email'] = lambda:read_mail(mail)
            # Successfully resolved alternate addresses receive frequent checks.
            for row in health.report():
                name = row['replacement']
                if name:
                    provider,slug = name.split(': ',1)
                    fn = ashby if provider=='Ashby' else greenhouse
                    tasks[name] = lambda f=fn,b=slug:f(b)
            due = {name:fn for name,fn in tasks.items() if health.due(name)}
            with self.lock: self.state['skipped'] = len(tasks)-len(due)
            tasks = due
            with self.lock: self.state['total'] = len(tasks)
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(fn):name for name,fn in tasks.items()}
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        new = db.ingest(name, future.result())
                        health.record(name,'ok',3600 if name=='Jobicy' else 900 if name=='Arbeitnow UK' else 0)
                        with self.lock:
                            self.state['new_alerts'] += len(new)
                            self.state['successful'] += 1
                    except Exception as exc:
                        code = getattr(exc, 'code', None)
                        error = f'{name}: {type(exc).__name__}' + (f' ({code})' if code else '')
                        status = health.failure(name,exc,board=name.startswith(('Ashby: ','Greenhouse: ')))
                        if status!='unavailable':
                            with self.lock: self.state['errors'].append(error)
                    finally:
                        with self.lock: self.state['completed'] += 1
            # Resolve platform migrations only to registry-listed alternate addresses,
            # and only after the alternate provider answers successfully.
            known = {(b['vendor'],b['slug']) for b in self.directory}
            attempts = 0
            for row in health.report():
                if row['status']!='unavailable' or row['replacement'] or attempts>=5: continue
                provider,slug = row['name'].split(': ',1)
                vendor = 'ashby' if provider=='Greenhouse' else 'greenhouse'
                alternate = ('Ashby: ' if vendor=='ashby' else 'Greenhouse: ')+slug
                if (vendor,slug) not in known or not health.due(alternate): continue
                attempts += 1
                try:
                    jobs = (ashby if vendor=='ashby' else greenhouse)(slug)
                    new = db.ingest(alternate,jobs)
                    health.record(alternate,'ok',0)
                    with db.db:
                        db.db.execute('UPDATE source_health SET replacement=? WHERE name=?',(alternate,row['name']))
                    with self.lock:
                        self.state['recovered'].append(row['name']+' → '+alternate)
                        self.state['new_alerts'] += len(new)
                except Exception as exc:
                    health.failure(alternate,exc,board=True)
            db.save_scan_cursor(cursor)
            with self.lock:
                self.state.update(cursor=cursor,last_finished=datetime.now(timezone.utc).isoformat(),
                    unavailable=[r for r in health.report() if r['status']=='unavailable'])
        finally:
            db.db.close()
            with self.lock: self.state['running'] = False
