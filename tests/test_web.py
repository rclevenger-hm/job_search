import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from core import Store
from web import create_app

class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name)
        self.client = self.app.test_client()
        self.client.get('/')
        with self.client.session_transaction() as s: self.csrf = s['csrf']
        self.scanner = self.app.extensions['scanner']
    def tearDown(self): self.temp.cleanup()
    def post(self,path,data):
        return self.client.post(path,data=dict(data,csrf=self.csrf))

    def test_csrf_and_validation(self):
        self.assertEqual(self.client.post('/dismiss',data={'key':'x'}).status_code,403)
        self.assertEqual(self.post('/application',dict(title='SRE',company='Acme',url='javascript:alert(1)',stage='Applied')).status_code,400)

    def test_application_persists_restart(self):
        response = self.post('/application',dict(title='<script>alert(1)</script>',company='Acme',url='https://example.com/job',
            applied_on='2026-10-25',stage='Interviewing',notes='Follow up'))
        self.assertEqual(response.status_code,302)
        restarted = create_app(self.temp.name).test_client()
        page = restarted.get('/applications').get_data(as_text=True)
        self.assertIn('Follow up',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertNotIn('<script>alert(1)</script>',page)

    def test_freshness_and_applied_visibility(self):
        db = Store(self.scanner.db_path)
        now = datetime.now(timezone.utc)
        base = dict(title='SRE',company='Acme',url='https://example.com/job',location='US Remote',source='test',eligibility='Ready')
        db.ingest('test',[dict(base,key='fresh',posted=now.isoformat()),
            dict(base,key='old',posted=(now-timedelta(days=2)).isoformat()),dict(base,key='unknown',posted='')])
        self.assertEqual(self.client.get('/api/status').json['available'],1)
        db.mark('fresh','Applied')
        self.assertEqual(self.client.get('/api/status').json['available'],0)
        self.assertIn('SRE',self.client.get('/applications').get_data(as_text=True))
        db.db.close()

    def test_mail_password_not_persisted(self):
        payload = dict(minutes='5',batch_size='150',boards='',ashby_boards='',wwr='on',mail_host='imap.example.com',
            mail_user='user@example.com',mail_folder='Jobs',password='test-private-value')
        self.assertEqual(self.post('/settings',payload).status_code,302)
        self.assertNotIn('test-private-value',self.scanner.settings_path.read_text())
        self.assertNotIn('test-private-value',self.client.get('/settings').get_data(as_text=True))
        self.assertIsNotNone(self.scanner.mail)
        self.assertIsNone(create_app(self.temp.name).extensions['scanner'].mail)

    @patch('scanner.remoteok',return_value=[])
    @patch('scanner.arbeitnow',return_value=[])
    @patch('scanner.weworkremotely',side_effect=TimeoutError)
    @patch('scanner.greenhouse',return_value=[])
    def test_scan_error_does_not_stop_other_sources(self,*mocks):
        self.scanner.directory = [dict(vendor='greenhouse',slug='one')]
        self.scanner.run_once()
        state = self.scanner.snapshot()
        self.assertFalse(state['running'])
        self.assertEqual(state['completed'],4)
        self.assertEqual(len(state['errors']),1)
        self.assertIsNotNone(state['last_finished'])

if __name__=='__main__': unittest.main()
