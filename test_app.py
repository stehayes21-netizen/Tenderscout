import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

FUTURE = "2099-12-31T00:00:00+00:00"


def feed_record(ocid, location_text, url=None, title="Cleaning contract",
                description="Routine cleaning services."):
    return {
        "source": "Find a Tender",
        "published_date": "2026-10-01",
        "title": title,
        "buyer": "Test buyer",
        "description": description,
        "location_text": location_text,
        "estimated_value_gbp": 100000,
        "deadline": FUTURE,
        "status": "tender",
        "notice_url": url or f"https://example.com/{ocid}",
        "ocid": ocid,
    }


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TENDERSCOUT_DB"] = str(Path(self.tmp.name) / "test.db")
        for name in ("DASHBOARD_USER", "DASHBOARD_PASSWORD", "DATABASE_URL"):
            os.environ.pop(name, None)
        import app
        self.app_module = importlib.reload(app)
        self.app_module.app.config["TESTING"] = True
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()
        for name in ("TENDERSCOUT_DB", "DASHBOARD_USER", "DASHBOARD_PASSWORD"):
            os.environ.pop(name, None)

    def refresh_with(self, records):
        collector = self.app_module.collector
        with patch.object(collector, "fetch_fts", return_value=records), \
             patch.object(collector, "fetch_contracts_finder", return_value=[]):
            return self.client.post("/refresh")


class AppSmokeTest(AppTestCase):
    def test_dashboard_loads(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Tender Scout", r.data)

    def test_manual_opportunity_persists_and_exports(self):
        r = self.client.post("/add", data={
            "title": "Test cleaning contract", "buyer": "Test buyer",
            "notice_url": "https://example.com/notice", "pipeline_status": "Reviewing",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Test cleaning contract", r.data)
        self.assertIn(b"Reviewing", r.data)
        exported = self.client.get("/export.csv")
        self.assertEqual(exported.status_code, 200)
        self.assertIn(b"Test cleaning contract", exported.data)

    def test_manual_entries_without_a_link_do_not_overwrite_each_other(self):
        for title in ("First entry", "Second entry"):
            self.client.post("/add", data={"title": title})
        page = self.client.get("/").data
        self.assertIn(b"First entry", page)
        self.assertIn(b"Second entry", page)

    def test_health_reports_database_type(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"status": "ok", "database": "sqlite"})


class RefreshTests(AppTestCase):
    def test_only_confirmed_target_area_tenders_reach_the_main_list(self):
        r = self.refresh_with([
            feed_record("liv", "Liverpool L2 2DH"),
            feed_record("edi", "Edinburgh EH1 1AA"),
            feed_record("unk", ""),
        ])
        self.assertEqual(r.status_code, 200)
        main = self.client.get("/").data
        self.assertIn(b"https://example.com/liv", main)
        self.assertNotIn(b"https://example.com/edi", main)
        self.assertNotIn(b"https://example.com/unk", main)

    def test_scottish_tenders_are_never_saved(self):
        self.refresh_with([feed_record("edi", "Edinburgh EH1 1AA")])
        self.assertNotIn(b"example.com/edi", self.client.get("/export.csv").data)
        self.assertNotIn(b"example.com/edi", self.client.get("/?view=review").data)

    def test_unclear_locations_wait_in_the_review_view(self):
        self.refresh_with([feed_record("unk", "")])
        self.assertNotIn(b"https://example.com/unk", self.client.get("/").data)
        review = self.client.get("/?view=review").data
        self.assertIn(b"https://example.com/unk", review)

    def test_refresh_keeps_the_pipeline_stage_you_set(self):
        self.refresh_with([feed_record("liv", "Liverpool L2 2DH")])
        db = self.app_module.connect_db()
        db.execute("UPDATE tenders SET pipeline_status = 'Bidding'")
        db.commit()
        db.close()
        self.refresh_with([feed_record("liv", "Liverpool L2 2DH")])
        self.assertIn(b"Bidding", self.client.get("/").data)

    def test_empty_feeds_are_reported_as_a_failure_not_zero_tenders(self):
        r = self.refresh_with([])
        self.assertEqual(r.status_code, 502)
        self.assertIn(b"does not mean", r.data)

    def test_api_refresh_alias_works(self):
        collector = self.app_module.collector
        with patch.object(collector, "fetch_fts", return_value=[feed_record("liv", "WA8 7AA")]), \
             patch.object(collector, "fetch_contracts_finder", return_value=[]):
            r = self.client.post("/api/refresh")
        self.assertEqual(r.status_code, 200)

    def test_csv_export_neutralises_spreadsheet_formulas(self):
        self.client.post("/add", data={"title": "=HYPERLINK(\"http://x\")"})
        exported = self.client.get("/export.csv").data.decode()
        self.assertIn("'=HYPERLINK", exported)


class LoginTests(AppTestCase):
    def test_site_is_open_when_no_password_is_configured(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_site_requires_login_once_password_is_configured(self):
        os.environ["DASHBOARD_USER"] = "admin"
        os.environ["DASHBOARD_PASSWORD"] = "s3cret"
        self.assertEqual(self.client.get("/").status_code, 401)
        self.assertEqual(self.client.post("/refresh").status_code, 401)
        self.assertEqual(self.client.post("/add", data={"title": "x"}).status_code, 401)
        self.assertEqual(self.client.get("/export.csv").status_code, 401)

    def test_correct_login_is_accepted_and_wrong_one_rejected(self):
        os.environ["DASHBOARD_USER"] = "admin"
        os.environ["DASHBOARD_PASSWORD"] = "s3cret"
        import base64

        def header(creds):
            return {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}

        self.assertEqual(self.client.get("/", headers=header("admin:s3cret")).status_code, 200)
        self.assertEqual(self.client.get("/", headers=header("admin:wrong")).status_code, 401)

    def test_health_check_stays_open_for_the_host(self):
        os.environ["DASHBOARD_USER"] = "admin"
        os.environ["DASHBOARD_PASSWORD"] = "s3cret"
        self.assertEqual(self.client.get("/health").status_code, 200)


class DashboardFeatureTests(AppTestCase):
    def test_refresh_button_is_styled_as_a_button_with_a_loading_state(self):
        page = self.client.get("/").data
        self.assertIn(b'class="btn"', page)
        self.assertIn(b"Searching", page)

    def test_stage_can_be_changed_and_is_remembered(self):
        self.refresh_with([feed_record("liv", "Liverpool L2 2DH")])
        db = self.app_module.connect_db()
        tender_id = db.execute("SELECT id FROM tenders").fetchone()["id"]
        db.close()
        r = self.client.post(f"/tender/{tender_id}/stage", data={"stage": "Bidding"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(b"<option selected>Bidding</option>", self.client.get("/").data)

    def test_unknown_stage_is_rejected(self):
        self.client.post("/add", data={"title": "x"})
        r = self.client.post("/tender/1/stage", data={"stage": "Hacked"})
        self.assertEqual(r.status_code, 400)

    def test_stage_redirect_cannot_be_pointed_at_another_site(self):
        self.client.post("/add", data={"title": "x"})
        r = self.client.post("/tender/1/stage", data={
            "stage": "Won", "view": "https://evil.example", "service": "//evil.example"})
        self.assertEqual(r.headers["Location"], "/")

    def test_closing_dates_are_labelled(self):
        info = self.app_module.deadline_info
        self.assertEqual(info("")[0], "Check official notice")
        self.assertTrue(info("2000-01-01T00:00:00+00:00")[2])
        self.assertTrue(info("2000-01-01T00:00:00+00:00")[0].startswith("Closed"))
        self.assertIn("days", info("2099-12-31T00:00:00+00:00")[0])

    def test_closed_tenders_sink_below_open_ones(self):
        old = feed_record("old", "Liverpool L2 2DH")
        open_ = feed_record("open", "Liverpool L2 2DH")
        self.refresh_with([old, open_])
        db = self.app_module.connect_db()
        db.execute("UPDATE tenders SET deadline = '2000-01-01T00:00:00+00:00', score = 99 "
                   "WHERE notice_url LIKE '%/old'")
        db.commit()
        db.close()
        page = self.client.get("/").data.decode()
        self.assertLess(page.index("example.com/open"), page.index("example.com/old"))
        self.assertIn("Closed 1 Jan 2000", page)

    def test_service_filter_only_shows_that_service(self):
        self.refresh_with([
            feed_record("clean", "Liverpool L2 2DH", title="Office cleaning"),
            feed_record("fm", "Liverpool L2 2DH", title="Facilities management services",
                        description="Hard FM and building maintenance."),
        ])
        cleaning = self.client.get("/?service=cleaning").data
        self.assertIn(b"example.com/clean", cleaning)
        self.assertNotIn(b"example.com/fm", cleaning)

    def test_post_from_another_website_is_blocked(self):
        r = self.client.post("/refresh", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
