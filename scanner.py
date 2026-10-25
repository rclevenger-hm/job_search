"""One background scanner per application process; SQLite connections stay thread-local."""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from core import (Store, greenhouse, ashby, arbeitnow, weworkremotely, remoteok,
                  read_mail, load_board_directory, directory_batch)

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
        self.directory = load_board_directory(directory or Path(__file__).with_name('board-directory.json'))
        db = Store(self.db_path)
        cursor = db.scan_cursor()
        db.db.close()
        self.state = dict(running=False, completed=0, total=0, errors=[], last_finished=None,
                          new_alerts=0, cursor=cursor, directory_size=len(self.directory))

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
            self.state.update(running=True, completed=0, errors=[], new_alerts=0)
        db = Store(self.db_path)
        try:
            batch, cursor = directory_batch(self.directory, db.scan_cursor(), settings['batch_size'])
            tasks = {'Arbeitnow':arbeitnow, 'Remote OK':remoteok}
            if settings['wwr']: tasks['We Work Remotely'] = weworkremotely
            for b in batch:
                fn = ashby if b['vendor']=='ashby' else greenhouse
                name = ('Ashby: ' if b['vendor']=='ashby' else 'Greenhouse: ') + b['slug']
                tasks[name] = lambda f=fn, slug=b['slug']: f(slug)
            for key, prefix, fn in [('boards','Greenhouse: ',greenhouse),('ashby_boards','Ashby: ',ashby)]:
                for slug in settings[key]: tasks[prefix+slug] = lambda f=fn,b=slug:f(b)
            if mail: tasks['LinkedIn email'] = lambda:read_mail(mail)
            with self.lock: self.state['total'] = len(tasks)
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(fn):name for name,fn in tasks.items()}
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        new = db.ingest(name, future.result())
                        with self.lock: self.state['new_alerts'] += len(new)
                    except Exception as exc:
                        code = getattr(exc, 'code', None)
                        error = f'{name}: {type(exc).__name__}' + (f' ({code})' if code else '')
                        with self.lock: self.state['errors'].append(error)
                    finally:
                        with self.lock: self.state['completed'] += 1
            db.save_scan_cursor(cursor)
            with self.lock:
                self.state.update(cursor=cursor,last_finished=datetime.now(timezone.utc).isoformat())
        finally:
            db.db.close()
            with self.lock: self.state['running'] = False
