import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import eligibility, linkedin_url, parse_email, Store, read_mail, ROLE, posted_age, posted_within_24_hours
from core import ashby, parse_wwr
from core import load_board_directory, directory_batch, remoteok

class Tests(unittest.TestCase):
    def test_directory_rotation_and_validation(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'directory.json'
            path.write_text(json.dumps([{'vendor':'ashby','slug':'one'},{'vendor':'greenhouse','slug':'two'},
                {'vendor':'ashby','slug':'one'},{'vendor':'ashby','slug':'../bad'}, {'vendor':'other','slug':'three'}]))
            boards = load_board_directory(path)
            self.assertEqual(len(boards),2)
            batch, cursor = directory_batch(boards,0,1)
            self.assertEqual(batch[0]['slug'],'one')
            batch, cursor = directory_batch(boards,cursor,1)
            self.assertEqual(batch[0]['slug'],'two')
            self.assertEqual(cursor,0)
            store = Store(Path(tmp)/'test.db')
            store.save_scan_cursor(150)
            store.db.close()
            store = Store(Path(tmp)/'test.db')
            self.assertEqual(store.scan_cursor(),150)
            store.db.close()

    @patch('core.fetch_json')
    def test_remoteok(self, fetch):
        fetch.return_value = [{'legal':'attribution'}, dict(id=1,position='DevOps Engineer',company='Example',
            location='USA',url='https://remoteok.com/remote-jobs/one',date='2026-10-25T10:00:00Z')]
        jobs = remoteok()
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['source'],'Remote OK')
        self.assertEqual(jobs[0]['eligibility'],'Ready')

    @patch('core.fetch_json')
    def test_ashby_remote_and_listed(self, fetch):
        job = dict(id='1',title='SRE',isListed=True,workplaceType='Remote',isRemote=True,
            location='Remote',address={'postalAddress':{'addressCountry':'USA'}},
            publishedAt='2026-10-25T10:00:00Z',jobUrl='https://jobs.ashbyhq.com/company/1')
        fetch.return_value = {'jobs':[job,dict(job,id='2',workplaceType='Hybrid'),dict(job,id='3',isListed=False)]}
        rows = ashby('company')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['eligibility'],'Ready')
        self.assertEqual(rows[0]['posted'],job['publishedAt'])

    def test_wwr_feed(self):
        xml = '''<rss><channel><item><title>Example: DevOps Engineer</title>
            <region>USA</region><pubDate>Sun, 25 Oct 2026 06:00:00 -0500</pubDate>
            <link>https://weworkremotely.com/remote-jobs/example</link></item>
            <item><title>Example: SRE</title><region>Germany</region>
            <pubDate>bad date</pubDate><link>https://weworkremotely.com/remote-jobs/second</link></item>
            <item><title>Example: Sales</title></item></channel></rss>'''
        rows = parse_wwr(xml)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['eligibility'],'Ready')
        self.assertEqual(rows[0]['source'],'We Work Remotely')
        self.assertEqual(rows[0]['posted'],'2026-10-25T06:00:00-05:00')
        self.assertEqual(rows[1]['eligibility'],'Excluded')
        self.assertFalse(posted_within_24_hours(rows[1]['posted']))

    def test_age_cutoff(self):
        now = datetime(2026,10,25,12,tzinfo=timezone.utc)
        self.assertTrue(posted_within_24_hours((now-timedelta(hours=24,seconds=-1)).isoformat(),now))
        self.assertFalse(posted_within_24_hours((now-timedelta(hours=24)).isoformat(),now))
        self.assertFalse(posted_within_24_hours((now-timedelta(hours=25)).isoformat(),now))
        self.assertFalse(posted_within_24_hours((now+timedelta(seconds=1)).isoformat(),now))
        self.assertFalse(posted_within_24_hours('',now))
        self.assertFalse(posted_within_24_hours('invalid',now))
        self.assertFalse(posted_within_24_hours('2026-10-25T11:00:00',now))
        self.assertTrue(posted_within_24_hours('2026-10-25T06:00:00-05:00',now))
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'jobs.db')
            store.ingest('x',[])
            job = dict(key='old',title='SRE',company='x',location='US Remote',url='https://example.com/jobs/1',source='x',eligibility='Ready',posted=(datetime.now(timezone.utc)-timedelta(days=91)).isoformat())
            self.assertEqual(store.ingest('x',[job]),[])
            self.assertEqual(store.ingest('x',[dict(job,key='unknown',posted='')]),[])
            store.mark('old','Applied')
            self.assertEqual(len(store.applications()),1)
            self.assertEqual(len(store.ingest('x',[dict(job,key='recent',posted=datetime.now(timezone.utc).isoformat())])),1)
            store.db.close()

    def test_posted_age(self):
        now = datetime(2026,10,25,12,tzinfo=timezone.utc)
        self.assertEqual(posted_age('2026-10-25T09:30:00Z',now),'2h ago')
        self.assertEqual(posted_age('2026-10-22T07:00:00-05:00',now),'3d ago')
        self.assertEqual(posted_age('',now),'Unknown')
        self.assertEqual(posted_age('bad date',now),'Unknown')
        self.assertEqual(posted_age('2026-10-25T12:00:00',now),'Unknown')
        self.assertEqual(posted_age('2026-10-26T12:00:00Z',now),'Future date')

    def test_application_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'jobs.db'
            store = Store(path)
            job = dict(key='gh:x:1',title='SRE',company='x',location='US Remote',url='https://example.com/jobs/1',source='Greenhouse',eligibility='Ready',posted='')
            store.ingest('x',[job])
            # Simulate an application saved with the old version.
            store.db.execute("UPDATE jobs SET status='Applied'")
            store.db.commit()
            store.db.close()
            store = Store(path)
            self.assertEqual(store.applications()[0]['applied_on'], '')
            store.save_application(job['key'],'SRE','x',job['url'],'2026-10-24','Interviewing','Call with recruiter')
            store.ingest('x',[dict(job,posted='2026-10-20T00:00:00Z')])
            self.assertEqual(store.rows()[0]['status'],'Applied')
            self.assertEqual(store.rows()[0]['posted'],'2026-10-20T00:00:00Z')
            store.mark(job['key'],'New')
            self.assertEqual(store.applications()[0]['stage'],'Interviewing')
            key = store.save_application(None,'DevOps Engineer','Other company','','2026-10-23','Applied','Via referral')
            with self.assertRaises(ValueError):
                store.save_application(key,'DevOps','Other','','2026-02-30','Applied','')
            store.db.close()
            store = Store(path)
            self.assertEqual(len(store.applications()),2)
            self.assertEqual(next(a for a in store.applications() if a['job_key']==job['key'])['notes'],'Call with recruiter')
            store.db.close()

    def test_remote_eligibility(self):
        for value in ['Remote - United States', 'USA (Remote)', 'Worldwide']:
            self.assertEqual(eligibility(value), 'Ready')
        self.assertEqual(eligibility('US Hybrid Remote'), 'Excluded')
        self.assertEqual(eligibility('Remote - Germany'), 'Excluded')
        self.assertEqual(eligibility('United States'), 'Review')
        self.assertEqual(eligibility('Remote'), 'Review')
        self.assertEqual(eligibility('USA', False), 'Excluded')

    def test_roles(self):
        for title in ['Senior Site Reliability Engineer', 'Staff SRE', 'DevOps Engineer', 'Platform Engineer II', 'Forward Deployed Engineer',
                      'Senior Forward-Deployed Engineer', 'Forward Deployed Software Engineer',
                      'Forward Deployed Infrastructure Engineer', 'Cloud Infrastructure Engineer',
                      'Senior Infrastructure Engineer', 'Cloud Engineer', 'Cloud Platform Engineer',
                      'Production Engineer', 'Production Reliability Engineer',
                      'Developer Productivity Engineer', 'Developer Experience Engineer',
                      'Build and Release Engineer', 'Build & Release Engineer', 'Release Engineer',
                      'CI/CD Engineer', 'CI-CD Engineer', 'DevSecOps Engineer', 'Dev Sec Ops Engineer',
                      'Software Engineer, Infrastructure', 'Engineer - Developer Productivity']:
            self.assertTrue(ROLE.search(title))
        self.assertFalse(ROLE.search('Account Executive'))
        self.assertFalse(ROLE.search('Forward Deployed Sales Consultant'))
        for title in ['Cloud Account Manager', 'Developer Advocate', 'Production Operator', 'Release Coordinator']:
            self.assertFalse(ROLE.search(title),title)

    def test_link_boundary(self):
        self.assertIsNone(linkedin_url('https://linkedin.com.evil.test/jobs/view/123'))
        self.assertIsNone(linkedin_url('javascript:alert(1)'))
        self.assertEqual(linkedin_url('https://www.linkedin.com/jobs/view/senior-sre-123?tracking=abc'), 'https://www.linkedin.com/jobs/view/123')

    def test_email_no_cross_card_location(self):
        raw = b'Content-Type: text/html; charset=utf-8\r\n\r\n<a href="https://www.linkedin.com/jobs/view/123?tracking=a">Senior DevOps Engineer</a><div>Germany</div><a href="https://www.linkedin.com/jobs/view/456">Account Executive</a><div>Remote USA</div>'
        jobs = parse_email(raw)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['eligibility'], 'Review')

    def test_persistence_baseline_and_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'jobs.db'
            store = Store(path)
            job = dict(key='gh:x:1',title='SRE',company='x',location='US Remote',url='https://example.com/jobs/1',source='Greenhouse',eligibility='Ready',posted='')
            self.assertEqual(store.ingest('x',[job]), [])
            store.db.close()
            store = Store(path)
            self.assertEqual(store.ingest('x',[job]), [])
            second = dict(job,key='gh:x:2',url='https://example.com/jobs/2',posted=datetime.now(timezone.utc).isoformat())
            self.assertEqual(len(store.ingest('x',[second])),1)
            store.mark(second['key'],'Applied')
            store.ingest('x',[second])
            self.assertEqual(next(r for r in store.rows() if r['key']==second['key'])['status'],'Applied')
            store.db.close()

    @patch('core.imaplib.IMAP4_SSL')
    def test_mail_is_readonly(self, ssl):
        mail = MagicMock()
        ssl.return_value.__enter__.return_value = mail
        mail.select.return_value = ('OK',[])
        mail.uid.side_effect = [('OK',[b'11']),('OK',[(b'meta',b'Content-Type: text/plain\r\n\r\nhello')])]
        self.assertEqual(read_mail(('mail.example.com','user','secret','LinkedIn Jobs')), [])
        mail.select.assert_called_once_with('"LinkedIn Jobs"', readonly=True)
        self.assertEqual(mail.uid.call_args_list[-1].args, ('fetch',b'11','(BODY.PEEK[])'))

if __name__ == '__main__': unittest.main()
