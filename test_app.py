import importlib
from io import BytesIO
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
        os.environ["DASHBOARD_USER"] = "test-user"
        os.environ["DASHBOARD_PASSWORD"] = "test-password"
        import app
        self.app_module = importlib.reload(app)
        self.app_module.app.config["TESTING"] = True
        self.client = self.app_module.app.test_client()
        import base64
        self.client.environ_base["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(b"test-user:test-password").decode()

    def tearDown(self):
        self.tmp.cleanup()
        for name in ("TENDERSCOUT_DB", "DASHBOARD_USER", "DASHBOARD_PASSWORD"):
            os.environ.pop(name, None)

    def refresh_with(self, records):
        collector = self.app_module.collector
        # Patch the executor as well as the feeds: the production refresh uses
        # worker threads, while tests should execute these deterministic fixtures
        # synchronously and avoid thread/mocking interactions.
        class InlineFuture:
            def __init__(self, fn, args, kwargs):
                self.value = fn(*args, **kwargs)
            def result(self):
                return self.value

        class InlineExecutor:
            def __init__(self, *args, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def submit(self, fn, *args, **kwargs):
                return InlineFuture(fn, args, kwargs)

        with patch.object(self.app_module, "ThreadPoolExecutor", InlineExecutor), \
             patch.object(collector, "fetch_fts", side_effect=lambda *args, **kwargs: records), \
             patch.object(collector, "fetch_contracts_finder", side_effect=lambda *args, **kwargs: []), \
             patch.object(collector, "fetch_bearings", side_effect=lambda *args, **kwargs: []), \
             patch.object(collector, "fetch_standstill_northwest", side_effect=lambda *args, **kwargs: []):
            return self.client.post("/refresh", follow_redirects=True)


class AppSmokeTest(AppTestCase):
    def test_dashboard_loads(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Tender Scout", r.data)

    def test_main_board_hides_expired_and_non_bidding_notices(self):
        for title in ("Expired cleaning tender", "Award notice for cleaning"):
            self.client.post("/add", data={
                "title": title,
                "buyer": "Test Council",
                "notice_url": "https://example.com/" + title.lower().replace(" ", "-"),
            })
        db = self.app_module.connect_db()
        db.execute(
            "UPDATE tenders SET region_match = 'LOCAL', deadline = ?, opportunity_stage = ? WHERE title = ?",
            ("2026-10-01T12:00:00Z", "Open for bids", "Expired cleaning tender"),
        )
        db.execute(
            "UPDATE tenders SET region_match = 'LOCAL', deadline = ?, opportunity_stage = ? WHERE title = ?",
            ("2099-12-31T00:00:00Z", "Other notice", "Award notice for cleaning"),
        )
        db.commit()
        db.close()

        page = self.client.get("/").data.decode()
        self.assertNotIn("Expired cleaning tender", page)
        self.assertNotIn("Award notice for cleaning", page)
        # Filtering the active board must not delete the records from storage.
        exported = self.client.get("/export.csv").data.decode()
        self.assertIn("Expired cleaning tender", exported)
        self.assertIn("Award notice for cleaning", exported)

    def test_review_queue_keeps_relevant_notice_when_deadline_is_missing(self):
        record = feed_record(
            "review-no-deadline",
            "Greater Manchester",
            title="Cleaning services framework",
            description="Commercial cleaning for public buildings.",
        )
        record["deadline"] = ""
        response = self.refresh_with([record])
        self.assertEqual(response.status_code, 200)
        review_page = self.client.get("/?view=review").data.decode()
        self.assertIn("Cleaning services framework", review_page)
        self.assertIn("Check official notice", review_page)
        main_page = self.client.get("/").data.decode()
        self.assertNotIn("Cleaning services framework", main_page)

    def test_jobs_board_shows_scope_and_contract_period_without_expanding_card(self):
        self.refresh_with([feed_record(
            "board-visible-1",
            "Liverpool, L1 1AA",
            title="Commercial cleaning at local offices",
            description="Daily office cleaning, washroom servicing and periodic deep cleaning.",
        )])
        page = self.client.get("/").data.decode()
        self.assertIn("10-second check", page)
        self.assertIn("JOB:", page)
        self.assertIn("Daily office cleaning, washroom servicing and periodic deep cleaning.", page)
        self.assertIn("WHERE:", page)
        self.assertIn("VALUE / DEADLINE / CONTRACT:", page)
        summary = page[page.index('<section class="board-summary">'):page.index('</section>', page.index('<section class="board-summary">'))]
        self.assertEqual(summary.count("<p>"), 3)
        self.assertLess(page.index('<section class="board-summary">'), page.index('<details class="full-details">'))

    def test_location_review_notice_with_target_area_clue_is_visible_on_main_board_as_unverified(self):
        record = feed_record(
            "review-local-clue",
            "",
            title="Cleaning services in Liverpool",
            description="Commercial cleaning framework; exact delivery site is not supplied.",
        )
        self.refresh_with([record])
        page = self.client.get("/").data.decode()
        self.assertIn("Cleaning services in Liverpool", page)
        self.assertIn("POTENTIAL LOCAL — verify delivery site", page)
        review = self.client.get("/?view=review").data.decode()
        self.assertIn("Cleaning services in Liverpool", review)

    def test_local_buyer_can_surface_a_review_candidate_without_confirming_delivery_site(self):
        record = feed_record(
            "review-local-buyer",
            "North West England",
            title="Commercial cleaning framework",
            description="Cleaning services; exact delivery site must be checked.",
        )
        record["buyer"] = "Liverpool City Council"
        self.refresh_with([record])
        page = self.client.get("/").data.decode()
        self.assertIn("Commercial cleaning framework", page)
        self.assertIn("POTENTIAL LOCAL — verify delivery site", page)
        self.assertIn("North West England", page)

    def test_private_lead_filters_by_trade_and_status(self):
        self.client.post("/leads", data={
            "title": "Roof repair", "trade_category": "Roofing and roof repairs",
            "location_text": "Liverpool L15", "source_name": "Direct customer enquiry",
            "details": "Repair a leaking roof",
        })
        self.client.post("/leads", data={
            "title": "Gutter clearance", "trade_category": "Gutter cleaning",
            "location_text": "Sefton L23", "source_name": "Direct customer enquiry",
            "details": "Clear blocked gutters",
        })
        db = self.app_module.connect_db()
        db.execute("UPDATE private_leads SET status = 'Passed to trade' WHERE title = 'Roof repair'")
        db.commit()
        db.close()
        filtered = self.client.get("/leads?trade=Roofing%20and%20roof%20repairs&status=Passed%20to%20trade").data.decode()
        self.assertIn("Roof repair", filtered)
        self.assertNotIn("Gutter clearance", filtered)
        self.assertIn("WHERE:", filtered)
        self.assertIn("WHAT THE CUSTOMER NEEDS", filtered)
        self.assertIn("Customer / business contact", filtered)
        self.assertIn("Source status:", filtered)

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

    def test_pipeline_view_only_shows_upcoming_pipeline_opportunities(self):
        self.client.post("/add", data={
            "title": "Planned cleaning contract", "buyer": "Council",
            "notice_url": "https://example.com/pipeline",
            "pipeline_status": "Reviewing",
            "opportunity_stage": "Planning / upcoming",
        })
        self.client.post("/add", data={
            "title": "Live cleaning tender", "buyer": "Council",
            "notice_url": "https://example.com/live",
            "pipeline_status": "Bidding",
        })
        db = self.app_module.connect_db()
        db.execute(
            "UPDATE tenders SET trade_category = ? WHERE notice_url = ?",
            ("Commercial and office cleaning", "https://example.com/pipeline"),
        )
        db.execute(
            "UPDATE tenders SET opportunity_stage = ? WHERE notice_url = ?",
            ("Planning / upcoming", "https://example.com/pipeline"),
        )
        db.execute(
            "UPDATE tenders SET trade_category = ? WHERE notice_url = ?",
            ("Commercial and office cleaning", "https://example.com/live"),
        )
        db.commit()
        db.close()
        pipeline = self.client.get("/?view=pipeline")
        self.assertEqual(pipeline.status_code, 200)
        self.assertIn(b"Planned cleaning contract", pipeline.data)
        self.assertNotIn(b"Live cleaning tender", pipeline.data)
        self.assertIn(b"Reviewing", pipeline.data)
        filtered = self.client.get("/?view=pipeline&trade=Commercial%20and%20office%20cleaning")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(b"Planned cleaning contract", filtered.data)
        self.assertNotIn(b"Live cleaning tender", filtered.data)

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


    def test_source_directory_lists_council_and_community_sources(self):
        r = self.client.get("/sources")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"ProContract", r.data)
        self.assertIn(b"The Chest", r.data)
        self.assertIn(b"HomeGuild", r.data)
        self.assertIn(b"BuildPair", r.data)
        self.assertIn(b"Formby Bubble", r.data)
        self.assertNotIn(b"Wirral Council", r.data)
        self.assertIn(b"does not automatically scrape Facebook", r.data)

    def test_buildconnect_rss_imports_only_target_area_public_projects(self):
        feed = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <item><title>Building refurbishment project in Liverpool</title>
            <description>Painting, repairs and refurbishment work in Liverpool L15.</description>
            <link>https://www.buildconnect-uk.co.uk/projects/liverpool-refurbishment</link>
            <pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item>
          <item><title>Roofing project in the Wirral</title>
            <description>Roof repairs in Birkenhead, Wirral.</description>
            <link>https://www.buildconnect-uk.co.uk/projects/wirral-roofing</link>
          </item>
          <item><title>Manchester extension</title>
            <description>Home extension in Manchester.</description>
            <link>https://www.buildconnect-uk.co.uk/projects/manchester-extension</link>
          </item>
        </channel></rss>"""
        with patch.object(self.app_module, "urlopen", return_value=BytesIO(feed)):
            response = self.client.post("/leads/import-buildconnect", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        page = response.data.decode()
        self.assertIn("Building refurbishment project in Liverpool", page)
        self.assertIn("Send by WhatsApp", page)
        self.assertIn("Location is only a clue", page)
        # Share links must contain real line breaks, not literal backslash-n text.
        self.assertIn("%0A", page)
        self.assertNotIn("%5Cn", page)
        self.assertIn("Roofing project in the Wirral", page)
        self.assertNotIn("Manchester extension", page)
        db = self.app_module.connect_db()
        count = db.execute("SELECT COUNT(*) AS n FROM external_projects").fetchone()["n"]
        db.close()
        self.assertEqual(count, 2)

    def test_private_lead_is_saved_separately_from_public_tenders(self):
        r = self.client.post("/leads", data={
            "title": "Repair leaking roof",
            "trade_category": "Roofing and roof repairs",
            "location_text": "Liverpool L15",
            "source_name": "Facebook community group",
            "source_url": "https://example.com/request",
            "details": "Customer requests a roof inspection",
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Private lead saved", r.data)
        self.assertIn(b"Repair leaking roof", r.data)
        self.assertNotIn(b"Repair leaking roof", self.client.get("/").data)

    def test_private_lead_has_urgency_and_one_tap_whatsapp_email_sharing(self):
        r = self.client.post("/leads", data={
            "title": "Urgent gutter cleaning",
            "trade_category": "Gutter cleaning",
            "location_text": "Allerton, Liverpool L18",
            "source_name": "Facebook community group",
            "source_url": "https://example.com/allerton-job",
            "contact_details": "Customer contact shared in original post",
            "details": "Clear blocked gutters today",
            "urgency": "Urgent — today",
        })
        self.assertEqual(r.status_code, 200)
        page = r.data.decode()
        self.assertIn("Urgent — today", page)
        self.assertIn("Exact job location", page)
        self.assertIn("Send lead by WhatsApp", page)
        self.assertIn("Send lead by email", page)
        self.assertIn("https://example.com/allerton-job", page)
        self.assertIn("Gutter cleaning", page)
        self.assertIn("JOB%3A+Urgent+gutter+cleaning", page)
        self.assertIn("CONTACT%3A+Customer+contact+shared+in+original+post", page)

    def test_private_lead_status_can_be_updated(self):
        self.client.post("/leads", data={
            "title": "Fallen tree", "trade_category": "Tree surgery",
            "location_text": "West Derby, Liverpool", "source_name": "Direct customer enquiry",
            "details": "Tree blown over in garden", "urgency": "Urgent — today",
        })
        db = self.app_module.connect_db()
        lead_id = db.execute("SELECT id FROM private_leads").fetchone()["id"]
        db.close()
        r = self.client.post(f"/leads/{lead_id}/status", data={"status": "Passed to trade"})
        self.assertEqual(r.status_code, 302)
        db = self.app_module.connect_db()
        status = db.execute("SELECT status FROM private_leads WHERE id = ?", (lead_id,)).fetchone()["status"]
        db.close()
        self.assertEqual(status, "Passed to trade")
        page = self.client.get("/leads").data.decode()
        self.assertIn("Fallen tree", page)
        self.assertIn("West Derby, Liverpool", page)
        self.assertIn("Passed to trade", page)
        self.assertIn('action="/leads/1/status"', page)
        self.assertIn("Lead progress", page)
        self.assertIn("Added", page)

    def test_private_leads_can_be_exported_as_csv_with_formula_protection(self):
        self.client.post("/leads", data={
            "title": "=HYPERLINK(\"https://bad.example\",\"click\")",
            "trade_category": "Roofing and roof repairs",
            "location_text": "Liverpool L15",
            "source_name": "Direct customer enquiry",
            "source_url": "https://example.com/lead",
            "contact_details": "Customer asked for a callback",
            "details": "Repair a leaking roof",
            "urgency": "This week",
        })
        response = self.client.get("/leads/export.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        self.assertIn("attachment; filename=tenderscout_private_leads.csv",
                      response.headers.get("Content-Disposition", ""))
        csv_text = response.data.decode("utf-8")
        self.assertIn("trade_category", csv_text)
        self.assertIn("Liverpool L15", csv_text)
        self.assertIn("https://example.com/lead", csv_text)
        self.assertIn("'=HYPERLINK", csv_text)

    def test_private_lead_rejects_unknown_trade_category(self):
        r = self.client.post("/leads", data={
            "title": "Job", "trade_category": "Not a real category",
            "location_text": "Liverpool", "source_name": "Community group",
        })
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Private lead saved", r.data)
        self.assertIn(b"valid trade", r.data)


    def test_trade_taxonomy_has_71_categories_and_classifies_roofing(self):
        import tenderscout
        self.assertEqual(len(tenderscout.TRADE_CATEGORIES), 71)
        self.assertEqual(
            tenderscout.classify_trade_category("Roofing and roof repairs"),
            "Roofing and roof repairs",
        )

    def test_public_tenders_can_be_filtered_by_detailed_trade(self):
        # Isolate the dashboard filter from feed-fetching behavior: seed two
        # valid local records, then verify only the selected detailed trade shows.
        self.client.post("/add", data={
            "title": "Roofing and roof repairs", "buyer": "Test buyer",
            "notice_url": "https://example.com/roof",
        })
        self.client.post("/add", data={
            "title": "Office cleaning", "buyer": "Test buyer",
            "notice_url": "https://example.com/clean",
        })
        db = self.app_module.connect_db()
        db.execute(
            "UPDATE tenders SET trade_category = ? WHERE notice_url = ?",
            ("Roofing and roof repairs", "https://example.com/roof"),
        )
        db.execute(
            "UPDATE tenders SET trade_category = ? WHERE notice_url = ?",
            ("Commercial and office cleaning", "https://example.com/clean"),
        )
        db.commit()
        db.close()

        page = self.client.get("/?trade=Roofing%20and%20roof%20repairs").data
        self.assertIn(b"example.com/roof", page)
        self.assertNotIn(b"example.com/clean", page)
        self.assertIn(b"Browse all 71 trade categories", page)

    def test_empty_trade_filter_explains_no_matches_and_offers_all_trades(self):
        self.client.post("/add", data={
            "title": "Office cleaning", "buyer": "Test buyer",
            "notice_url": "https://example.com/clean",
        })
        db = self.app_module.connect_db()
        db.execute(
            "UPDATE tenders SET trade_category = ? WHERE notice_url = ?",
            ("Commercial and office cleaning", "https://example.com/clean"),
        )
        db.commit()
        db.close()

        page = self.client.get("/?trade=Roofing%20and%20roof%20repairs").data
        self.assertIn(b"No saved opportunities match the trade", page)
        self.assertIn(b"Show all trades", page)
        self.assertIn(b"example.com/clean", self.client.get("/").data)
        self.assertNotIn(b"example.com/clean", page)

    def test_private_lead_trade_selector_uses_shared_71_category_list(self):
        import tenderscout
        page = self.client.get("/leads").data
        self.assertEqual(len(tenderscout.TRADE_CATEGORIES), 71)
        self.assertIn(b"Rope access and specialist access", page)
        self.assertIn(b"Environmental services", page)


class RefreshTests(AppTestCase):
    def test_confirmed_and_review_tenders_are_visible_on_jobs_board(self):
        r = self.refresh_with([
            feed_record("liv", "Liverpool L2 2DH"),
            feed_record("edi", "Edinburgh EH1 1AA"),
            feed_record("unk", ""),
        ])
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Refresh complete.", r.data)
        main = self.client.get("/").data
        self.assertIn(b"https://example.com/liv", main)
        self.assertNotIn(b"https://example.com/edi", main)
        self.assertNotIn(b"https://example.com/unk", main)
        self.assertNotIn(b"Location needs checking", main)
        self.assertIn(b"https://example.com/unk", self.client.get("/?view=review").data)

    def test_empty_dashboard_points_to_saved_location_review_tenders(self):
        self.refresh_with([feed_record("unk", "")])
        page = self.client.get("/").data
        self.assertNotIn(b"https://example.com/unk", page)
        self.assertIn(b"https://example.com/unk", self.client.get("/?view=review").data)
        self.assertIn(b"Full location review (1)", page)
        self.assertIn(b"view=review", page)

    def test_scottish_tenders_are_never_saved(self):
        self.refresh_with([feed_record("edi", "Edinburgh EH1 1AA")])
        self.assertNotIn(b"example.com/edi", self.client.get("/export.csv").data)
        self.assertNotIn(b"example.com/edi", self.client.get("/?view=review").data)

    def test_explicit_worldwide_scope_is_not_put_in_local_review_queue(self):
        result = self.app_module.collector.classify_geography(
            {"location_text": "North West, British Overseas Territories, Channel Islands, Europe, Isle of Man, Rest of the World"},
            self.app_module.collector.TARGET_AREA_LABEL,
        )
        self.assertEqual(result[0], "OUT_OF_AREA")

    def test_unclear_locations_wait_in_the_review_view(self):
        self.refresh_with([feed_record("unk", "")])
        self.assertNotIn(b"https://example.com/unk", self.client.get("/").data)
        review = self.client.get("/?view=review").data
        self.assertIn(b"https://example.com/unk", review)

    def test_job_board_card_shows_description_and_public_buyer_contact(self):
        record = feed_record("contact-card", "Liverpool L2 2DH")
        record.update({
            "contact_name": "Procurement Team",
            "contact_email": "procurement@example.gov.uk",
            "contact_phone": "0151 555 0100",
            "eligibility_text": "Registered cleaning providers",
            "estimated_value_gbp": 125000,
            "contract_start_date": "2027-04-01T00:00:00Z",
            "contract_end_date": "2029-03-31T23:59:59Z",
        })
        self.refresh_with([record])
        page = self.client.get("/").data.decode()
        self.assertIn("Routine cleaning services.", page)
        self.assertIn("Procurement Team", page)
        self.assertIn("procurement@example.gov.uk", page)
        self.assertIn("Registered cleaning providers", page)
        self.assertIn("Who can bid", page)
        self.assertIn("Where is the work?", page)
        self.assertIn("£125,000", page)
        self.assertIn("From 1 April 2027 to 31 March 2029", page)
        self.assertIn("How do you apply?", page)

    def test_refresh_updates_source_publication_and_match_metadata_without_resetting_pipeline(self):
        first = feed_record("metadata-refresh", "Liverpool L2 2DH", title="Commercial cleaning contract")
        first["published_date"] = "2026-10-01"
        self.refresh_with([first])
        db = self.app_module.connect_db()
        db.execute("UPDATE tenders SET pipeline_status = 'Bidding' WHERE notice_url = ?", ("https://example.com/metadata-refresh",))
        db.commit()
        db.close()

        updated = feed_record("metadata-refresh", "Liverpool L2 2DH", title="Roof repair contract", description="Roof repairs and maintenance.")
        updated["source"] = "Contracts Finder updated"
        updated["published_date"] = "2026-10-25"
        self.refresh_with([updated])
        db = self.app_module.connect_db()
        row = db.execute("SELECT source, published_date, service_match, pipeline_status FROM tenders WHERE notice_url = ?", ("https://example.com/metadata-refresh",)).fetchone()
        db.close()
        self.assertEqual(row["source"], "Contracts Finder updated")
        self.assertEqual(row["published_date"], "2026-10-25")
        self.assertIn("Property maintenance", row["service_match"])
        self.assertEqual(row["pipeline_status"], "Bidding")
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
    def test_site_is_locked_when_password_is_not_configured(self):
        os.environ.pop("DASHBOARD_USER", None)
        os.environ.pop("DASHBOARD_PASSWORD", None)
        self.assertEqual(self.client.get("/").status_code, 503)
        self.assertIn(b"locked until", self.client.get("/").data)

    def test_site_requires_login_without_valid_credentials(self):
        os.environ["DASHBOARD_USER"] = "admin"
        os.environ["DASHBOARD_PASSWORD"] = "s3cret"
        self.client.environ_base.pop("HTTP_AUTHORIZATION", None)
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

    def test_closed_tenders_are_hidden_from_active_jobs_board(self):
        old = feed_record("old", "Liverpool L2 2DH")
        open_ = feed_record("open", "Liverpool L2 2DH")
        self.refresh_with([old, open_])
        db = self.app_module.connect_db()
        db.execute("UPDATE tenders SET deadline = '2000-01-01T00:00:00+00:00', score = 99 "
                   "WHERE notice_url LIKE '%/old'")
        db.commit()
        db.close()
        page = self.client.get("/").data.decode()
        self.assertIn("example.com/open", page)
        self.assertNotIn("example.com/old", page)
        # Expired records remain stored for audit/export; only the active board hides them.
        exported = self.client.get("/export.csv").data.decode()
        self.assertIn("example.com/old", exported)

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


class RefreshSummaryTests(AppTestCase):
    def test_refresh_returns_to_the_dashboard_with_a_summary(self):
        collector = self.app_module.collector
        # Keep this test deterministic and offline: empty mocked feeds must
        # produce a failure response rather than a false "zero tenders" success.
        with patch.object(collector, "fetch_fts", return_value=[]), \
             patch.object(collector, "fetch_contracts_finder", return_value=[]), \
             patch.object(collector, "fetch_bearings", return_value=[]), \
             patch.object(collector, "fetch_standstill_northwest", return_value=[]):
            r = self.client.post("/refresh")
        self.assertEqual(r.status_code, 502)
        self.assertIn(b"does not mean there are no tenders", r.data)
        records = [
            feed_record("liv", "Liverpool L2 2DH"),
            feed_record("edi", "Edinburgh EH1 1AA"),
            feed_record("unk", ""),
            feed_record("other", "Liverpool L2 2DH", title="Software licences",
                        description="Annual software licences."),
        ]
        with patch.object(collector, "fetch_fts", return_value=records), \
             patch.object(collector, "fetch_contracts_finder", return_value=[]), \
             patch.object(collector, "fetch_bearings", return_value=[]), \
             patch.object(collector, "fetch_standstill_northwest", return_value=[]):
            r = self.client.post("/refresh")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].startswith("/?done=1"))
        page = self.client.get(r.headers["Location"]).data.decode()
        self.assertIn("Refresh complete.", page)
        self.assertIn("4 notices checked. 3 matched your trades; 1 of those were outside your area.", page)
        self.assertIn("1 confirmed in your area, 1 need a location check.", page)

    def test_summary_banner_ignores_junk_values(self):
        r = self.client.get("/?done=1&n=abc")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"Refresh complete.", r.data)

    def test_api_refresh_returns_plain_text_for_scheduled_jobs(self):
        collector = self.app_module.collector
        with patch.object(collector, "fetch_fts", return_value=[feed_record("liv", "WA8 7AA")]), \
             patch.object(collector, "fetch_contracts_finder", return_value=[]):
            r = self.client.post("/api/refresh")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/plain")
        self.assertIn(b"1 confirmed in your area", r.data)


class FeedDiagnosticsTests(AppTestCase):
    def test_dashboard_shows_what_each_feed_returned(self):
        collector = self.app_module.collector

        def fake_fts(days, pages, limit, stats):
            stats.update({"pages": 1, "records": 100, "last_batch": 100, "more": False,
                          "error": "", "stopped": "", "first_date": "2026-10-11",
                          "last_date": "2026-10-12"})
            return [feed_record("liv", "Liverpool L2 2DH")]

        def fake_cf(days, pages, limit, stats):
            stats.update({"pages": 3, "records": 250, "last_batch": 50, "more": False,
                          "error": "", "stopped": "time limit"})
            return []

        with patch.object(collector, "fetch_fts", side_effect=fake_fts), \
             patch.object(collector, "fetch_contracts_finder", side_effect=fake_cf):
            r = self.client.post("/refresh", follow_redirects=True)
        page = r.data.decode()
        self.assertIn("Find a Tender: 100 notices in 1 page(s), published 2026-10-11 to 2026-10-12", page)
        self.assertIn("no next-page link came back, so results may be cut off", page)
        self.assertIn("Contracts Finder: 250 notices in 3 page(s)", page)
        self.assertIn("Stopped early (time limit)", page)

    def test_collector_records_pages_and_stops_when_no_next_link(self):
        import tenderscout
        payload = {"releases": [{"ocid": "a", "id": "1", "date": "2026-10-10T00:00:00Z",
                                 "tender": {"title": "Cleaning"}}]}
        stats = {}
        with patch.object(tenderscout, "get_json", return_value=payload):
            records = tenderscout.fetch_fts(7, 5, None, stats)
        self.assertEqual(len(records), 1)
        self.assertEqual((stats["pages"], stats["records"], stats["more"]), (1, 1, False))
        self.assertEqual(stats["first_date"], "2026-10-10")


class FeedPagingTests(unittest.TestCase):
    PAGE1 = {"releases": [{"ocid": "a", "id": "1", "date": "2026-10-10T00:00:00Z", "tender": {"title": "T1"}}],
             "links": {"next": "http://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?cursor=ABC%3D%3D&limit=100"}}
    PAGE2 = {"releases": [{"ocid": "b", "id": "2", "date": "2026-10-11T00:00:00Z", "tender": {"title": "T2"}}]}

    def test_next_page_uses_the_link_the_feed_gives(self):
        import tenderscout
        urls = []

        def fake_get(url, timeout=25):
            urls.append(url)
            return self.PAGE1 if len(urls) == 1 else self.PAGE2

        stats = {}
        with patch.object(tenderscout, "get_json", side_effect=fake_get), \
             patch.object(tenderscout.time, "sleep"):
            records = tenderscout.fetch_fts(7, 5, None, stats)
        self.assertEqual(len(records), 2)
        self.assertEqual(urls[1],
                         "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?cursor=ABC%3D%3D&limit=100")
        self.assertEqual(stats["pages"], 2)

    def test_links_to_other_websites_are_never_followed(self):
        import tenderscout
        payload = {"releases": [], "links": {"next": "https://evil.example/steal?cursor=x"}}
        cursor = tenderscout._next_cursor(payload)
        url = tenderscout._page_url(tenderscout.FTS_URL, {"limit": "100"}, cursor)
        self.assertTrue(url.startswith("https://www.find-tender.service.gov.uk/"))
        self.assertNotIn("evil.example", url)
        self.assertIsNone(tenderscout._next_cursor(
            {"releases": [], "links": {"next": "https://evil.example/steal"}}))

    def test_http_errors_include_the_feeds_explanation(self):
        import io, urllib.error, tenderscout
        error = urllib.error.HTTPError("https://x", 400, "Bad Request", {}, io.BytesIO(b'{"error":"cursor is invalid"}'))
        with patch.object(tenderscout.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as ctx:
                tenderscout.get_json("https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?limit=100")
        self.assertIn("cursor is invalid", str(ctx.exception))
        self.assertIn("HTTP 400", str(ctx.exception))


class SearchWindowTests(unittest.TestCase):
    def test_look_back_is_split_into_week_long_windows_newest_first(self):
        import tenderscout
        windows = tenderscout._windows(60)
        self.assertEqual(len(windows), 9)
        starts_and_ends = [(s, e) for s, e in windows]
        self.assertTrue(all(e > s for s, e in starts_and_ends))
        self.assertGreater(windows[0][1], windows[-1][1])  # newest first
        for (s1, _), (_, e2) in zip(windows, windows[1:]):
            self.assertEqual(s1, e2)  # no gaps between windows
        self.assertEqual(round((windows[0][1] - windows[-1][0]).days), 60)

    def test_each_window_is_searched_and_counted(self):
        import tenderscout
        calls = []

        def fake_get(url, timeout=25):
            calls.append(url)
            return {"releases": [{"ocid": f"o{len(calls)}", "id": str(len(calls)),
                                  "date": "2026-10-10T00:00:00Z", "tender": {"title": "T"}}]}

        stats = {}
        with patch.object(tenderscout, "get_json", side_effect=fake_get), \
             patch.object(tenderscout.time, "sleep"):
            records = tenderscout.fetch_contracts_finder(21, 5, None, stats)
        self.assertEqual(stats["windows"], 3)
        self.assertEqual(len(records), 3)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("publishedFrom=" in u and "publishedTo=" in u for u in calls))

    def test_time_limit_stops_after_the_newest_windows(self):
        import tenderscout
        calls = []

        def fake_get(url, timeout=25):
            calls.append(url)
            return {"releases": [{"ocid": f"o{len(calls)}", "id": "1", "tender": {"title": "T"}}]}

        stats = {}
        ticks = iter([0, 0, 0, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100])
        with patch.object(tenderscout, "get_json", side_effect=fake_get), \
             patch.object(tenderscout.time, "sleep"), \
             patch.object(tenderscout.time, "monotonic", side_effect=lambda: next(ticks)):
            tenderscout.fetch_fts(60, 5, 10, stats)
        self.assertEqual(stats["stopped"], "time limit")
        self.assertLess(stats["windows"], 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
