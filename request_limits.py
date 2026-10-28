"""Conservative host pacing shared by all employer-board requests."""
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse


class DeferredRequest(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__('Provider cooldown')


class HostLimits:
    HOSTS = {'boards-api.greenhouse.io','api.ashbyhq.com','api.lever.co'}

    def __init__(self):
        self.lock = threading.Lock()
        self.slots = {host:threading.BoundedSemaphore(3) for host in self.HOSTS}
        self.next_start = {}
        self.blocked_until = {}

    @contextmanager
    def request(self, url):
        host = urlparse(url).hostname
        if host not in self.HOSTS:
            yield
            return
        with self.slots[host]:
            while True:
                with self.lock:
                    now = time.monotonic()
                    blocked = self.blocked_until.get(host,0)-now
                    if blocked > 0: raise DeferredRequest(blocked)
                    delay = self.next_start.get(host,0)-now
                    if delay <= 0:
                        self.next_start[host] = now + .5
                        break
                time.sleep(min(delay,.5))
            try:
                yield
            except Exception as exc:
                if getattr(exc,'code',None)==429:
                    value=(getattr(exc,'headers',None) or {}).get('Retry-After','')
                    try: seconds=float(value)
                    except (TypeError,ValueError):
                        try: seconds=parsedate_to_datetime(value).timestamp()-time.time()
                        except (TypeError,ValueError): seconds=900
                    with self.lock:
                        self.blocked_until[host]=max(self.blocked_until.get(host,0),time.monotonic()+max(900,seconds))
                raise


LIMITS = HostLimits()
