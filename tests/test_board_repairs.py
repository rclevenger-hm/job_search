import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from core import Store
from source_health import Health
from board_repairs import load_audit, apply_audit, repaired_directory

class BoardRepairTests(unittest.TestCase):
    @patch('core.fetch_json')
    def test_repaired_board_with_null_addresses(self, fetch):
        from core import ashby
        fetch.return_value=dict(jobs=[dict(id='one',title='Site Reliability Engineer',isListed=True,
            workplaceType='Remote',location='United States',address=None,
            secondaryLocations=[dict(location='Remote',address=dict(postalAddress=None))],
            jobUrl='https://jobs.ashbyhq.com/example/one',publishedAt='2026-10-25T00:00:00Z')])
        self.assertEqual(ashby('example')[0]['eligibility'],'Ready')

    def test_scanner_uses_reviewed_replacement_instead_of_old_priority(self):
        from scanner import Scanner
        with tempfile.TemporaryDirectory() as tmp:
            scanner=Scanner(tmp)
            scanner.settings["curated"]=[]
            scanner.audit=dict(version=1,boards=[dict(name='Greenhouse: old',replacement='Ashby: fixed',reason='verified')])
            scanner.settings.update(boards=['old'],ashby_boards=[],lever_boards=[],wwr=False)
            with patch('scanner.refresh_directory',return_value=([dict(vendor='greenhouse',slug='old')],'',1)), \
                 patch('scanner.greenhouse') as dead, patch('scanner.ashby',return_value=[]) as fixed, \
                 patch('scanner.arbeitnow',return_value=[]), patch('scanner.arbeitnow_uk',return_value=[]), \
                 patch('scanner.remoteok',return_value=[]), patch('scanner.jobicy',return_value=[]), \
                 patch('scanner.himalayas',return_value=[]), patch('scanner.remotive',return_value=[]):
                scanner.run_once()
                dead.assert_not_called()
                fixed.assert_called_once_with('fixed')
                self.assertEqual(scanner.snapshot()['unavailable'],[])

    def test_shipped_audit_is_valid_and_complete(self):
        audit=load_audit()
        self.assertEqual(len(audit['boards']),724)
        self.assertEqual(sum(bool(r['replacement']) for r in audit['boards']),57)
        rows={r['name']:r for r in audit['boards']}
        self.assertEqual(rows['Greenhouse: hightouch']['replacement'],'Ashby: hightouch-inc')
        self.assertEqual(rows['Ashby: serif']['replacement'],'')

    def test_audit_clears_guesses_and_does_not_extend_retry_dates(self):
        db=Store(':memory:'); health=Health(db.db)
        health.record('Ashby: serif','unavailable',1,replacement='Greenhouse: serif')
        audit=dict(version=1,boards=[dict(name='Ashby: serif',replacement='',reason='404'),
            dict(name='Greenhouse: renamed',replacement='Ashby: new-name',reason='Verified')])
        apply_audit(health,audit)
        original=health.get('Ashby: serif')
        self.assertEqual(original['replacement'],'')
        self.assertEqual(health.get('Greenhouse: renamed')['status'],'replaced')
        apply_audit(health,audit)
        self.assertEqual(health.get('Ashby: serif'),original)
        db.db.close()

    def test_directory_substitution_and_deduplication(self):
        audit=dict(boards=[dict(name='Greenhouse: old',replacement='Ashby: new')])
        boards=[dict(vendor='greenhouse',slug='old'),dict(vendor='ashby',slug='new'),
                dict(vendor='lever',slug='other')]
        repaired=repaired_directory(boards,audit)
        self.assertEqual({(r['vendor'],r['slug']) for r in repaired},{('ashby','new'),('lever','other')})
        self.assertEqual(len(repaired),2)

    def test_rejects_invalid_targets_and_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'audit.json'
            for rows in ([dict(name='Ashby: old',replacement='Ashby: ../bad')],
                         [dict(name='Ashby: a',replacement='Lever: b'),dict(name='Lever: b',replacement='Ashby: a')]):
                path.write_text(json.dumps(dict(version=1,boards=rows)))
                with self.assertRaises(ValueError): load_audit(path)
