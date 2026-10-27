import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from curated import parse_detail, array_variable, monitor, COMPANIES, read_page
from web import create_app

def page(**values):
    data=dict({'@type':'JobPosting'},title='Site Reliability Engineer',jobLocationType='TELECOMMUTE',
              applicantLocationRequirements=[dict(name='United States')],**values)
    return '<script type="application/ld+json">'+json.dumps(data)+'</script>'

class CuratedTests(unittest.TestCase):
    def test_publication_and_email_instructions(self):
        result=parse_detail(page(datePosted='2026-10-27T12:00:00Z',description='Please apply by emailing careers@example.com with your resume.'),
            'https://example.com/careers/sre','Example','')
        self.assertEqual(result['posted'],'2026-10-27T12:00:00Z')
        self.assertIn('Email: careers@example.com',result['source_date'])

    def test_unknown_date_not_replaced_with_discovery(self):
        for date in ('','2026-10-27','bad'):
            result=parse_detail(page(datePosted=date),'https://example.com/job','Example','')
            self.assertEqual(result['posted'],'')

    def test_office_and_foreign_jobs_excluded(self):
        office='<h2>Site Reliability Engineer</h2><p>This is a full-time position based out of the Mountain View office.</p>'
        self.assertIsNone(parse_detail(office,'https://example.com/job','Example','SRE'))
        foreign=page().replace('United States','Germany')
        self.assertIsNone(parse_detail(foreign,'https://example.com/job','Example','SRE'))
        self.assertIsNone(parse_detail(page(validThrough='2020-01-01T00:00:00Z'),'https://example.com/job','Example','SRE'))

    def test_listing_is_data_not_executed_code(self):
        self.assertEqual(array_variable('const jobs = [{"title":"SRE"}];','jobs'),[dict(title='SRE')])
        with self.assertRaises(ValueError): array_variable('const jobs = evil();','jobs')
        with self.assertRaises(ValueError): read_page('http://localhost/','example.com')
        with self.assertRaises(ValueError): read_page('https://other.example/jobs','example.com')

    @patch('curated.read_page')
    def test_removed_detail_does_not_abort_company(self, read):
        read.side_effect=['const vacancies = [{"id":12,"slug":"sre","title":"SRE","location":"Home based - Worldwide"}, {"id":13,"slug":"sre","title":"SRE","location":"Home based - Worldwide"}];',
            HTTPError('https://canonical.com/careers/12',404,'Gone',{},None),page()]
        self.assertEqual([r['key'] for r in monitor(COMPANIES[0])],['gh:canonical:13'])

    @patch('curated.read_page')
    def test_direct_page_reuses_ats_job_identity(self, read):
        read.side_effect=['const vacancies = [{"id":12,"slug":"sre","title":"SRE","location":"Home based - Worldwide"}];',page()]
        rows=monitor(COMPANIES[0])
        self.assertEqual(rows[0]['key'],'gh:canonical:12')
        self.assertEqual(rows[0]['posted'],'')

    def test_watchlist_selection_persists_and_rejects_unknown_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=create_app(tmp); client=app.test_client()
            self.assertEqual(client.get('/monitor').status_code,200)
            with client.session_transaction() as session: token=session['csrf']
            self.assertEqual(client.post('/monitor',data=dict(csrf=token,company=['canonical','gitlab'])).status_code,302)
            self.assertEqual(create_app(tmp).extensions['scanner'].settings['curated'],['canonical','gitlab'])
            self.assertEqual(client.post('/monitor',data=dict(csrf=token,company=['https://localhost'])).status_code,400)
            self.assertEqual(client.post('/monitor',data=dict(company=['canonical'])).status_code,403)
