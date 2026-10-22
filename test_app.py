import os, tempfile, unittest
from pathlib import Path

class AppSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['TENDERSCOUT_DB'] = str(Path(self.tmp.name) / 'test.db')
        import importlib, app
        self.app_module = importlib.reload(app)
        self.app_module.app.config['TESTING'] = True
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop('TENDERSCOUT_DB', None)

    def test_dashboard_loads(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Tender Scout', r.data)

    def test_manual_opportunity_persists_and_exports(self):
        r = self.client.post('/add', data={'title':'Test cleaning contract','buyer':'Test buyer','notice_url':'https://example.com/notice','pipeline_status':'Reviewing'}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Test cleaning contract', r.data)
        self.assertIn(b'Reviewing', r.data)
        exported = self.client.get('/export.csv')
        self.assertEqual(exported.status_code, 200)
        self.assertIn(b'Test cleaning contract', exported.data)

if __name__ == '__main__': unittest.main(verbosity=2)
