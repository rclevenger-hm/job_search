import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from core import Store, ROLE, jobicy
from source_health import Health, refresh_directory

class HealthTests(unittest.TestCase):
    def test_persistent_quarantine_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'jobs.db'
            store=Store(path); health=Health(store.db)
            err=HTTPError('https://example.com',404,'missing',{},None)
            self.assertEqual(health.failure('Ashby: gone',err,board=True,now=100),'unavailable')
            store.db.close(); store=Store(path); health=Health(store.db)
            self.assertFalse(health.due('Ashby: gone',now=101))
            self.assertTrue(health.due('Ashby: gone',now=100+30*86400))
            health.record('Ashby: gone','ok',0,now=200)
            self.assertTrue(health.due('Ashby: gone',now=201))
            self.assertEqual(health.report(),[])
            store.db.close()

    def test_transient_and_feed_limits(self):
        store=Store(':memory:'); h=Health(store.db)
        h.failure('Greenhouse: slow',TimeoutError(),board=True,now=100)
        self.assertEqual(h.get('Greenhouse: slow')['status'],'error')
        h.failure('Jobicy',TimeoutError(),now=100)
        self.assertFalse(h.due('Jobicy',now=3699))
        h.failure('rate',HTTPError('https://example.com',429,'rate',{'Retry-After':'7200'},None),now=100)
        self.assertFalse(h.due('rate',now=7299))
        store.db.close()

    @patch('source_health.urllib.request.urlopen')
    def test_directory_cache_validation_and_refresh_failure(self, request):
        rows=[dict(status='ok',provider='ashby',slug='new',name='New'),
              dict(status='ok',provider='ashby',slug='../bad'),dict(status='dead',provider='ashby',slug='gone')]
        response=MagicMock(); response.read.return_value='\n'.join(json.dumps(r) for r in rows).encode()
        request.return_value.__enter__.return_value=response
        with tempfile.TemporaryDirectory() as tmp:
            seed=[dict(vendor='greenhouse',slug='old',company='Old')]
            boards,notice,_=refresh_directory(seed,tmp,now=100000)
            self.assertEqual(len(boards),2)
            refresh_directory(seed,tmp,now=100001)
            self.assertEqual(request.call_count,1)
            request.side_effect=TimeoutError()
            boards,notice,_=refresh_directory(seed,tmp,now=200000)
            self.assertEqual(len(boards),2)
            self.assertIn('keeping cached',notice)

    @patch('core.fetch_json')
    def test_jobicy_and_title_variants(self, fetch):
        fetch.return_value={'jobs':[dict(id=1,jobTitle='Software Engineer, Platform',jobGeo='USA',companyName='Example',
            url='https://jobicy.com/jobs/1',pubDate='2026-10-25T12:00:00Z')], 'nextCursor':None}
        rows=jobicy()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['eligibility'],'Ready')
        self.assertTrue(ROLE.search('Staff Engineer - Platform'))
        self.assertFalse(ROLE.search('Platform Sales Manager'))

if __name__=='__main__': unittest.main()
