import json
import time
import unittest
from unittest.mock import patch, MagicMock
from core import Store
from extra_sources import himalayas, lever, remotive, usajobs
from source_health import Health

class ExtraSourceTests(unittest.TestCase):
    @patch('extra_sources.fetch_json')
    def test_himalayas_country_expiry_dedup_and_no_false_freshness(self, fetch):
        base=dict(title='DevOps Engineer',companyName='Example',guid='https://himalayas.app/companies/example/jobs/devops',
                  applicationLink='https://himalayas.app/jobs/devops',pubDate=time.time(),expiryDate=time.time()+10000,
                  locationRestrictions=[dict(name='United States',alpha2='US')])
        fetch.return_value=dict(jobs=[base,dict(base,guid='foreign',locationRestrictions=['France']),
                                     dict(base,guid='expired',expiryDate=1)])
        jobs=himalayas()
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['posted'],'')
        self.assertIn('Last updated:',jobs[0]['source_date'])
        self.assertEqual(jobs[0]['eligibility'],'Ready')
        db=Store(':memory:')
        db.ingest('Himalayas',[])
        self.assertEqual(db.ingest('Himalayas',jobs),[])
        self.assertEqual(len(db.rows()),1)
        db.mark(jobs[0]['key'],'Applied')
        db.ingest('Himalayas',[dict(jobs[0],expires=1)])
        self.assertEqual(db.rows(),[])
        self.assertEqual(len(db.applications()),1)
        db.db.close()

    @patch('extra_sources.fetch_json')
    def test_lever_only_remote_us_and_creation_is_not_publication(self, fetch):
        base=dict(id='one',text='Site Reliability Engineer',country='US',workplaceType='remote',
                  categories=dict(location='Remote'),hostedUrl='https://jobs.lever.co/example/one',createdAt=time.time()*1000)
        fetch.return_value=[base,dict(base,id='hybrid',workplaceType='hybrid'),dict(base,id='foreign',country='FR')]
        jobs=lever('example')
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['posted'],'')
        self.assertIn('Created:',jobs[0]['source_date'])

    @patch('extra_sources.fetch_json')
    def test_remotive_delayed_feed_never_alerts(self, fetch):
        fetch.return_value=dict(jobs=[dict(id=1,title='DevOps Engineer',company_name='Example',
            candidate_required_location='USA',url='https://remotive.com/jobs/one',publication_date='2026-10-25T12:00:00')])
        job=remotive()[0]
        self.assertEqual(job['posted'],'')
        self.assertEqual(job['eligibility'],'Ready')
        self.assertGreater(job['expires'],time.time())

    @patch('extra_sources.urllib.request.urlopen')
    def test_usajobs_remote_request_and_timezone_conservatism(self, fetch):
        response=MagicMock()
        response.read.return_value=json.dumps(dict(SearchResult=dict(SearchResultItems=[dict(MatchedObjectId=1,
            MatchedObjectDescriptor=dict(PositionTitle='Platform Engineer',PositionURI='https://www.usajobs.gov/job/1',
                PublicationStartDate='2026-10-25',OrganizationName='Agency',PositionLocationDisplay='Anywhere in the U.S.'))]))).encode()
        fetch.return_value.__enter__.return_value=response
        stats={}
        jobs=usajobs('test@example.com','private-key',stats)
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['posted'],'')
        self.assertEqual(jobs[0]['expires'],0)
        self.assertEqual(stats['fetched'],1)
        self.assertEqual(stats['unknown_date'],1)
        req=fetch.call_args.args[0]
        self.assertIn('RemoteIndicator=True',req.full_url)
        self.assertEqual(req.get_header('Authorization-key'),'private-key')

    @patch('extra_sources.urllib.request.urlopen')
    def test_federal_deadlines_and_diagnostics(self, fetch):
        from datetime import datetime, timezone, timedelta
        now=datetime.now(timezone.utc)
        def item(key, title='DevOps Engineer', closing=None):
            return dict(MatchedObjectId=key,MatchedObjectDescriptor=dict(PositionTitle=title,
                PositionURI='https://www.usajobs.gov/job/'+str(key),PublicationStartDate=now.isoformat(),
                ApplicationCloseDate=closing or (now+timedelta(days=30)).isoformat()))
        response=MagicMock()
        response.read.return_value=json.dumps(dict(SearchResult=dict(SearchResultItems=[
            item(1),item(2,'Accountant'),item(3,closing=(now-timedelta(days=1)).isoformat())]))).encode()
        fetch.return_value.__enter__.return_value=response
        stats={}; jobs=usajobs('test@example.com','key',stats)
        self.assertEqual(len(jobs),1)
        self.assertGreater(jobs[0]['expires'],time.time()+86400)
        self.assertEqual((stats['fetched'],stats['title_rejected'],stats['closed'],stats['fresh']),(3,1,1,1))

    def test_feed_failure_minimum_intervals(self):
        db=Store(':memory:'); health=Health(db.db)
        for name,delay in [('Himalayas',86400),('Remotive',21600),('USAJOBS',3600)]:
            health.failure(name,TimeoutError(),now=100)
            self.assertFalse(health.due(name,now=100+delay-1))
        db.db.close()
