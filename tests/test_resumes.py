import io
import tempfile
import unittest
from docx import Document
from core import Store
from web import create_app


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=create_app(self.temp.name)
        self.client=self.app.test_client()
        self.client.get('/resumes')
        with self.client.session_transaction() as session: self.csrf=session['csrf']

    def tearDown(self): self.temp.cleanup()

    def post(self,path,data): return self.client.post(path,data=dict(data,csrf=self.csrf))

    def test_save_update_restart_download_and_delete(self):
        response=self.post('/resumes/new',dict(title='SRE draft',name='Alex Example',experience='- Improved service reliability'))
        self.assertEqual(response.status_code,302)
        self.assertIn('SRE draft',create_app(self.temp.name).test_client().get('/resumes').get_data(as_text=True))
        self.post('/resumes/1/edit',dict(title='Platform draft',name='Alex Example',experience='- Maintained Kubernetes'))
        docx=self.client.get('/resumes/1/download/docx')
        self.assertEqual(docx.status_code,200)
        text='\n'.join(p.text for p in Document(io.BytesIO(docx.data)).paragraphs)
        self.assertIn('Maintained Kubernetes',text)
        self.assertNotIn('Improved service reliability',text)
        pdf=self.client.get('/resumes/1/download/pdf')
        self.assertEqual(pdf.status_code,200)
        self.assertTrue(pdf.data.startswith(b'%PDF'))
        prompt=self.client.get('/resumes/1/refine').get_data(as_text=True)
        self.assertIn('Do not invent',prompt)
        self.assertIn('Maintained Kubernetes',prompt)
        self.assertEqual(self.client.post('/resumes/1/delete').status_code,403)
        self.assertEqual(self.client.get('/resumes/1/delete').status_code,405)
        self.assertEqual(self.post('/resumes/1/delete',{}).status_code,302)
        self.assertEqual(self.client.get('/resumes/1/download/pdf').status_code,404)

    def test_validation_and_html_escaping(self):
        self.assertEqual(self.post('/resumes/new',dict(title='')).status_code,400)
        self.assertEqual(self.post('/resumes/new',dict(title='draft',summary='x'*20001)).status_code,400)
        self.post('/resumes/new',dict(title='<script>bad()</script>',name='</textarea><script>bad()</script>'))
        for path in ['/resumes','/resumes/1/edit','/resumes/1/refine']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertNotIn('<script>bad()',response.get_data(as_text=True))
        self.assertEqual(self.client.get('/resumes/1/download/exe').status_code,404)

    def test_job_review_contains_original_metadata(self):
        store=Store(self.app.extensions['scanner'].db_path)
        store.ingest('test',[dict(key='sample',title='DevOps Engineer',company='Example',location='US Remote',
            url='https://example.com/job',source='Test',posted='',eligibility='Ready')])
        store.db.close()
        page=self.client.get('/?scope=browse').get_data(as_text=True)
        self.assertIn('Review in ChatGPT',page)
        response=self.client.get('/jobs/review?key=sample')
        self.assertEqual(response.status_code,200)
        self.assertIn('https://example.com/job',response.get_data(as_text=True))
        self.assertIn('not the full description',response.get_data(as_text=True))
        self.assertEqual(self.client.get('/jobs/review?key=missing').status_code,404)
