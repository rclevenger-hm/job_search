import html
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from core import Store, posted_within_24_hours
from web import create_app
import major_employers as m


class MajorEmployerTests(unittest.TestCase):
    def test_workday_keeps_first_total_and_all_remote_locations(self):
        posting=lambda n:dict(title='Platform Engineer',externalPath='/job/US/Test_'+str(n))
        def response(url,payload=None):
            if payload is not None:
                offset=payload['offset']
                return dict(total=21 if offset==0 else 0,jobPostings=[posting(n) for n in range(offset,min(offset+20,21))])
            return dict(jobPostingInfo=dict(jobReqId=url.rsplit('_',1)[1],title='Platform Engineer',
                location='US, CA, Santa Clara',additionalLocations=['US, WA, Remote','India, Remote'],startDate='2026-10-01'))
        with patch.object(m,'get_json',side_effect=response): rows=m.nvidia()
        self.assertEqual(len(rows),21)
        self.assertEqual(rows[0]['location'],'US, WA, Remote')
        self.assertEqual(rows[0]['posted'],'')
        self.assertIn('2026-10-01',rows[0]['source_date'])

    def test_amazon_requires_remote_us_location(self):
        def job(n,kind,country): return dict(id_icims=n,title='DevOps Engineer',job_path='/en/jobs/'+n,
            posted_date='October 1, 2026',locations=[json.dumps(dict(type=kind,countryIso2a=country,location='US, WA, Seattle'))])
        with patch.object(m,'get_json',return_value=dict(error=None,hits=3,jobs=[job('1','REMOTE','US'),job('2','ONSITE','US'),job('3','REMOTE','CA')])):
            rows=m.amazon()
        self.assertEqual(len(rows),1)
        self.assertFalse(posted_within_24_hours(rows[0]['posted']))

    def test_google_separates_remote_and_office_locations(self):
        jobs=[['1','Platform Engineer','<b>In-office locations: California, USA.</b><b>Remote location(s): Canada.</b>'],
              ['2','Site Reliability Engineer','<b>Remote location(s): Texas, USA.</b>']]
        raw="key: 'ds:1', data:"+json.dumps([jobs,None,2,20])
        with patch.object(m,'request',return_value=raw): rows=m.google()
        self.assertEqual(len(rows),1)
        self.assertIn('Texas',rows[0]['location'])

    def test_microsoft_verifies_applied_filter(self):
        with patch.object(m,'get_json',return_value=dict(data=dict(appliedFilters={},positions=[],count=0))):
            with self.assertRaises(ValueError): m.microsoft()

    def test_microsoft_remote_filter_and_posting_time(self):
        data=dict(appliedFilters=dict(workSite=['0 days / week in-office \u2013 remote']),count=2,positions=[
            dict(id=1,name='Platform Engineer',locations=['United States, Multiple Locations'],postedTs=1700000000,positionUrl='/careers/job/1'),
            dict(id=2,name='Platform Engineer',locations=['Canada'],postedTs=1700000000,positionUrl='/careers/job/2')])
        with patch.object(m,'get_json',return_value=dict(data=data)): rows=m.microsoft()
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['posted'].endswith('+00:00'))

    def test_apple_requires_us_home_office(self):
        def job(n,remote,country): return dict(id=n,positionId=n,postingTitle='SRE',homeOffice=remote,
            transformedPostingTitle='sre',postDateInGMT='2026-10-01T12:00:00Z',locations=[dict(name=country,countryName=country)])
        payload=dict(search=dict(totalRecords=3,searchResults=[job('1',True,'United States'),job('2',False,'United States'),job('3',True,'Canada')]))
        raw='window.__staticRouterHydrationData = JSON.parse('+json.dumps(json.dumps(payload))+')'
        with patch.object(m,'request',return_value=raw): rows=m.apple()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['posted'],'2026-10-01T12:00:00+00:00')

    def test_netflix_partial_and_no_creation_date_alert(self):
        data=dict(count=50,positions=[dict(id=1,name='Platform Engineer',locations=['Remote, United States'],
            work_location_option='remote',canonicalPositionUrl='https://explore.jobs.netflix.net/careers/job/1',t_create=123)])
        raw='<code id="smartApplyData">'+html.escape(json.dumps(data))+'</code>'
        with patch.object(m,'request',return_value=raw): rows=m.netflix()
        self.assertTrue(rows.report['partial'])
        self.assertEqual(rows[0]['posted'],'')

    def test_partial_rate_limit_retains_rows_and_retry_after(self):
        def interrupted():
            batch=m.Batch();batch.add('nvidia','1','SRE',['US Remote'],'https://example.com/job')
            raise HTTPError('https://example.com',429,'limited',{'Retry-After':'7200'},None)
        with patch.object(m,'nvidia',side_effect=interrupted): rows=m.fetch('nvidia')
        self.assertEqual(len(rows),1)
        self.assertTrue(rows.report['partial'])
        self.assertEqual(rows.report['retry_seconds'],7200)
        with self.assertRaises(m.ConnectorUnavailable): m.fetch('meta')

    def test_major_page_filters_preserves_old_jobs_and_alert_window(self):
        with tempfile.TemporaryDirectory() as folder:
            app=create_app(folder);client=app.test_client();db=Store(app.extensions['scanner'].db_path)
            batch=m.Batch();batch.add('nvidia','1','Platform Engineer',['US Remote'],'https://example.com/old','2025-01-01')
            batch.add('microsoft','2','DevOps Engineer',['US Remote'],'https://example.com/unknown')
            db.ingest('test',batch);db.db.close()
            page=client.get('/major-employers?company=nvidia').get_data(as_text=True)
            self.assertIn('Platform Engineer',page);self.assertNotIn('DevOps Engineer',page)
            self.assertIn('2025-01-01',page)
            self.assertNotIn('Platform Engineer',client.get('/major-employers?age=7').get_data(as_text=True))
            self.assertIn('DevOps Engineer',client.get('/major-employers?age=unknown').get_data(as_text=True))
            self.assertEqual(client.get('/api/status').json['available'],0)
            with client.session_transaction() as session: csrf=session['csrf']
            self.assertEqual(client.post('/major-employers',data=dict(csrf=csrf,employer=['nvidia'])).status_code,302)
            self.assertEqual(create_app(folder).extensions['scanner'].settings['major_employers'],['nvidia'])
            self.assertEqual(client.post('/major-employers',data=dict(csrf=csrf,employer=['evil'])).status_code,400)
