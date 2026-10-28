import tempfile
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

from core import Store
from request_limits import HostLimits, DeferredRequest
from scanner import Scanner


class SchedulingTests(unittest.TestCase):
    def test_slow_feed_does_not_hold_directory_or_overwrite_cursor(self):
        entered, release = threading.Event(), threading.Event()
        def slow():
            entered.set()
            if not release.wait(5): raise TimeoutError()
            return []
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            scanner=Scanner(folder)
            scanner.audit=dict(version=1,boards=[])
            scanner.settings.update(curated=[],batch_size=1)
            scanner.seed_directory=[dict(vendor='greenhouse',slug='one'),dict(vendor='greenhouse',slug='two')]
            scanner.directory=scanner.seed_directory
            stack.enter_context(patch('scanner.refresh_directory',return_value=(scanner.seed_directory,'',None)))
            board=stack.enter_context(patch('scanner.greenhouse',return_value=[]))
            for name in ['arbeitnow','arbeitnow_uk','remoteok','jobicy','remotive','weworkremotely']:
                stack.enter_context(patch('scanner.'+name,return_value=[]))
            stack.enter_context(patch('scanner.himalayas',side_effect=slow))
            thread=threading.Thread(target=scanner.run_once,args=('feeds',))
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                scanner.run_once('directory')
                self.assertTrue(scanner.snapshot()['feeds']['running'])
                self.assertEqual(scanner.snapshot()['cursor'],1)
                board.assert_called_once_with('one')
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            db=Store(scanner.db_path)
            self.assertEqual(db.scan_cursor(),1)
            db.db.close()

    def test_directory_pause_is_five_seconds_feed_interval_is_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            scanner=Scanner(folder)
            scanner.wake=MagicMock(); scanner.feed_wake=MagicMock()
            for lane,expected,event in [('directory',5,scanner.wake),('feeds',300,scanner.feed_wake)]:
                scanner.stop.clear()
                with patch.object(scanner,'run_once',side_effect=lambda lane:scanner.stop.set()):
                    scanner.loop(lane)
                event.wait.assert_called_once_with(expected)

    def test_provider_429_defers_other_boards_without_request(self):
        limiter=HostLimits()
        with patch('request_limits.time.monotonic',return_value=100):
            with self.assertRaises(HTTPError):
                with limiter.request('https://api.ashbyhq.com/one'):
                    raise HTTPError('https://api.ashbyhq.com/one',429,'limited',{'Retry-After':'1800'},None)
            with self.assertRaises(DeferredRequest) as error:
                with limiter.request('https://api.ashbyhq.com/two'):
                    self.fail('Must not execute another request')
            self.assertEqual(error.exception.seconds,1800)
            with limiter.request('https://boards-api.greenhouse.io/one'): pass

    def test_request_starts_are_paced(self):
        limiter=HostLimits()
        with patch('request_limits.time.monotonic',side_effect=[100,100,100.5]), patch('request_limits.time.sleep') as sleep:
            with limiter.request('https://api.lever.co/one'): pass
            with limiter.request('https://api.lever.co/two'): pass
            sleep.assert_called_once_with(.5)
