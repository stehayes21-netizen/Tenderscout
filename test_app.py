import base64
import importlib
from io import BytesIO
import os
import sqlite3
import threading
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
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
    def test_legacy_discovery_duplicate_is_removed_and_pipeline_status_preserved(self):
        db = self.app_module.connect_db()
        db.execute(
            "INSERT INTO tenders (source,title,buyer,published_date,deadline,notice_url,region_match,opportunity_stage,pipeline_status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("Find a Tender", "Roof repairs in Liverpool", "Example Council", "2026-10-27",
             "2099-11-20T17:00:00Z", "https://official.example/notice", "REVIEW", "Open for bids", "Reviewing"),
        )
        db.execute(
            "INSERT INTO tenders (source,title,buyer,published_date,deadline,notice_url,region_match,opportunity_stage,pipeline_status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("Bearings (discovery; verify provider notice)", "Roof repairs in Liverpool", "Example Council",
             "2026-10-20", "2099-11-20", "https://discovery.example/notice", "REVIEW", "Other notice", "Interested"),
        )
        removed = self.app_module.remove_cross_source_duplicates(db)
        db.commit()
        rows = db.execute("SELECT source,pipeline_status FROM tenders").fetchall()
        db.close()
        self.assertEqual(removed, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "Find a Tender")
        self.assertEqual(rows[0]["pipeline_status"], "Interested")

    def test_dashboard_loads(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Tender Scout", r.data)

    def test_dashboard_refresh_uses_tracked_background_status(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'id="refresh-form"', r.data)
        self.assertIn(b'id="refresh-status"', r.data)
        self.assertIn(b"/api/refresh", r.data)
        self.assertIn(b"/api/refresh/status", r.data)

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
        self.assertNotIn("Expired cleaning tender", exported)
        self.assertNotIn("Award notice for cleaning", exported)
        full_audit = self.client.get("/export.csv?view=all").data.decode()
        self.assertIn("Expired cleaning tender", full_audit)
        self.assertIn("Award notice for cleaning", full_audit)

    def test_review_queue_keeps_relevant_notice_when_deadline_is_missing(self):
        record = feed_record(
            "review-no-deadline",
            "Greater Manchester",
            title="Cleaning services framework for Liverpool sites",
            description="Commercial cleaning for public buildings.",
        )
        record["deadline"] = ""
        response = self.refresh_with([record])
        self.assertEqual(response.status_code, 200)
        review_page = self.client.get("/?view=review").data.decode()
        self.assertIn("Cleaning services framework", review_page)
        self.assertIn("Check official notice", review_page)
        main_page = self.client.get("/").data.decode()
        self.assertIn("Cleaning services framework", main_page)
        self.assertIn("POTENTIAL LOCAL — verify delivery site", main_page)

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
            feed_record("review", "", title="Cleaning services in Liverpool"),
        ])
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Refresh complete.", r.data)
        main = self.client.get("/").data
        self.assertIn(b"https://example.com/liv", main)
        self.assertNotIn(b"https://example.com/edi", main)
        self.assertNotIn(b"https://example.com/unk", main)
        self.assertNotIn(b"Location needs checking", main)
        review_page = self.client.get("/?view=review").data
        self.assertIn(b"https://example.com/review", review_page)
        self.assertNotIn(b"https://example.com/unk", review_page)

    def test_empty_dashboard_hides_review_notices_without_a_local_clue(self):
        self.refresh_with([feed_record("unk", "")])
        page = self.client.get("/").data
        self.assertNotIn(b"https://example.com/unk", page)
        self.assertNotIn(b"https://example.com/unk", self.client.get("/?view=review").data)
        self.assertIn(b"Full location review (0)", page)

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

    def test_unclear_locations_without_a_target_area_clue_are_hidden(self):
        self.refresh_with([feed_record("unk", "")])
        self.assertNotIn(b"https://example.com/unk", self.client.get("/").data)
        review = self.client.get("/?view=review").data
        self.assertNotIn(b"https://example.com/unk", review)

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

    def test_api_refresh_alias_starts_background_job(self):
        collector = self.app_module.collector

        with patch.object(self.app_module, "_start_refresh_thread", side_effect=self.app_module._run_background_refresh), \
             patch.object(collector, "fetch_fts", return_value=[feed_record("liv", "WA8 7AA")]), \
             patch.object(collector, "fetch_contracts_finder", return_value=[]), \
             patch.object(collector, "fetch_bearings", return_value=[]), \
             patch.object(collector, "fetch_standstill_northwest", return_value=[]):
            r = self.client.post("/api/refresh")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json["status"], "running")
        status = self.client.get("/api/refresh/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["status"], "completed")

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
        # Expired records remain stored for the full audit, not the working export.
        exported = self.client.get("/export.csv").data.decode()
        self.assertNotIn("example.com/old", exported)
        full_audit = self.client.get("/export.csv?view=all").data.decode()
        self.assertIn("example.com/old", full_audit)

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

    def test_api_refresh_status_recovers_stale_running_state(self):
        db = self.app_module.connect_db()
        started = (self.app_module.datetime.now(self.app_module.timezone.utc)
                   - timedelta(minutes=20)).isoformat()
        db.execute(
            "UPDATE refresh_state SET status = 'running', started_at = ?, finished_at = NULL, summary = '', error = '', refresh_token = ? WHERE id = 1",
            (started, "stale-test-token"),
        )
        db.commit()
        db.close()

        response = self.client.get("/api/refresh/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "failed")
        self.assertIn("interrupted", response.json["error"])
        self.assertIsNotNone(response.json["finished_at"])

    def test_database_lease_prevents_a_second_process_starting_refresh(self):
        db = self.app_module.connect_db()
        started = self.app_module.datetime.now(self.app_module.timezone.utc).isoformat()
        db.execute(
            "UPDATE refresh_state SET status = 'running', started_at = ?, refresh_token = ? WHERE id = 1",
            (started, "active-process-token"),
        )
        db.commit()
        db.close()

        with patch.object(self.app_module, "_start_refresh_thread") as start_thread:
            response = self.client.post("/api/refresh")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "running")
        start_thread.assert_not_called()
        db = self.app_module.connect_db()
        state = db.execute("SELECT status, refresh_token FROM refresh_state WHERE id = 1").fetchone()
        db.close()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["refresh_token"], "active-process-token")

    def test_recent_heartbeat_prevents_reclaim_even_when_started_at_is_old(self):
        db = self.app_module.connect_db()
        started = (self.app_module.datetime.now(self.app_module.timezone.utc)
                   - timedelta(minutes=25)).isoformat()
        heartbeat = self.app_module.datetime.now(self.app_module.timezone.utc).isoformat()
        db.execute(
            "UPDATE refresh_state SET status = 'running', started_at = ?, heartbeat_at = ?, refresh_token = ? WHERE id = 1",
            (started, heartbeat, "still-active-token"),
        )
        db.commit()
        db.close()

        with patch.object(self.app_module, "_start_refresh_thread") as start_thread:
            response = self.client.post("/api/refresh")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "running")
        start_thread.assert_not_called()

    def test_heartbeat_renews_only_the_current_running_owner(self):
        db = self.app_module.connect_db()
        db.execute(
            "UPDATE refresh_state SET status = 'running', refresh_token = ?, heartbeat_at = NULL WHERE id = 1",
            ("heartbeat-owner",),
        )
        db.commit()
        db.close()
        class OneCycleEvent:
            def __init__(self):
                self.calls = 0
            def wait(self, seconds):
                self.calls += 1
                return self.calls > 1
        stop = OneCycleEvent()
        self.app_module._refresh_heartbeat("heartbeat-owner", stop)
        db = self.app_module.connect_db()
        state = db.execute("SELECT heartbeat_at FROM refresh_state WHERE id = 1").fetchone()
        db.close()
        self.assertIsNotNone(state["heartbeat_at"])

    def test_legacy_refresh_route_respects_an_existing_database_lease(self):
        db = self.app_module.connect_db()
        started = self.app_module.datetime.now(self.app_module.timezone.utc).isoformat()
        db.execute(
            "UPDATE refresh_state SET status = 'running', started_at = ?, heartbeat_at = ?, refresh_token = ? WHERE id = 1",
            (started, started, "active-api-refresh"),
        )
        db.commit()
        db.close()
        with patch.object(self.app_module, "refresh_or_error") as run_refresh:
            response = self.client.post("/refresh")
        self.assertEqual(response.status_code, 302)
        self.assertIn("refresh_started=1", response.headers["Location"])
        run_refresh.assert_not_called()

    def test_stale_database_lease_can_be_reclaimed_after_process_crash(self):
        db = self.app_module.connect_db()
        started = (self.app_module.datetime.now(self.app_module.timezone.utc)
                   - timedelta(minutes=20)).isoformat()
        db.execute(
            "UPDATE refresh_state SET status = 'running', started_at = ?, refresh_token = ? WHERE id = 1",
            (started, "dead-process-token"),
        )
        db.commit()
        db.close()

        with patch.object(self.app_module, "_start_refresh_thread", side_effect=lambda token: None) as start_thread:
            response = self.client.post("/api/refresh")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["status"], "running")
        start_thread.assert_called_once()
        new_token = start_thread.call_args.args[0]
        self.assertNotEqual(new_token, "dead-process-token")
        db = self.app_module.connect_db()
        state = db.execute("SELECT status, refresh_token FROM refresh_state WHERE id = 1").fetchone()
        db.close()
        self.assertEqual(state["refresh_token"], new_token)

    def test_simultaneous_refresh_requests_only_start_one_worker(self):
        # Initialise the schema before the two requests race for the same row.
        db = self.app_module.connect_db()
        db.close()
        barrier = threading.Barrier(2)
        started_tokens = []
        outcomes = []
        errors = []
        auth = "Basic " + base64.b64encode(b"test-user:test-password").decode()

        def request_refresh():
            try:
                with self.app_module.app.test_client() as client:
                    barrier.wait(timeout=5)
                    response = client.post("/api/refresh", headers={"Authorization": auth})
                    outcomes.append((response.status_code, response.json))
            except Exception as exc:
                errors.append(exc)

        with patch.object(
            self.app_module, "_start_refresh_thread",
            side_effect=lambda token: started_tokens.append(token),
        ):
            workers = [threading.Thread(target=request_refresh) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers), "refresh requests did not finish")
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual([status for status, _ in outcomes], [202, 202])
        self.assertEqual(len(started_tokens), 1, "only one request may start a refresh worker")
        self.assertEqual(sum("started_at" in body for _, body in outcomes), 1)

    def test_old_refresh_state_table_is_migrated_without_losing_its_row(self):
        # Recreate the pre-token schema to exercise a real upgrade, not just a
        # fresh database where CREATE TABLE already includes refresh_token.
        connection = sqlite3.connect(self.app_module.DB_PATH)
        connection.execute("DROP TABLE IF EXISTS refresh_state")
        connection.execute(
            """CREATE TABLE refresh_state (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                summary TEXT,
                error TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO refresh_state (id, status, summary) VALUES (1, 'failed', 'previous result')"
        )
        connection.commit()
        connection.close()
        self.app_module._schema_ready = False

        db = self.app_module.connect_db()
        columns = {row[1] for row in db.execute("PRAGMA table_info(refresh_state)").fetchall()}
        state = db.execute(
            "SELECT status, summary, refresh_token FROM refresh_state WHERE id = 1"
        ).fetchone()
        db.close()

        self.assertIn("refresh_token", columns)
        self.assertIn("heartbeat_at", columns)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["summary"], "previous result")
        self.assertIsNone(state["refresh_token"])

    def test_malformed_running_timestamp_is_reclaimed_like_stale_status_detection(self):
        db = self.app_module.connect_db()
        db.execute(
            "UPDATE refresh_state SET status = 'running', started_at = ?, refresh_token = ? WHERE id = 1",
            ("not-a-valid-timestamp", "malformed-old-token"),
        )
        db.commit()
        db.close()

        with patch.object(self.app_module, "_start_refresh_thread") as start_thread:
            response = self.client.post("/api/refresh")

        self.assertEqual(response.status_code, 202)
        start_thread.assert_called_once()
        self.assertNotEqual(start_thread.call_args.args[0], "malformed-old-token")

    def test_old_refresh_cannot_overwrite_state_owned_by_new_refresh(self):
        db = self.app_module.connect_db()
        db.execute(
            "UPDATE refresh_state SET status = 'running', refresh_token = ? WHERE id = 1",
            ("new-owner-token",),
        )
        db.commit()
        db.close()

        updated = self.app_module._set_refresh_state(
            "failed", error="old worker finished late", refresh_token="old-owner-token"
        )
        self.assertFalse(updated)
        db = self.app_module.connect_db()
        state = db.execute("SELECT status, refresh_token FROM refresh_state WHERE id = 1").fetchone()
        db.close()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["refresh_token"], "new-owner-token")

    def test_partial_feed_collection_is_reported_incomplete(self):
        self.app_module.LAST_FEEDS.clear()
        self.app_module.LAST_FEEDS["Find a Tender"] = {
            "records": 2410, "pages": 26, "windows": 15,
            "truncated_windows": 12, "stopped": "time limit",
            "error": "", "more": True,
        }
        with patch.object(self.app_module, "refresh_or_error", return_value=(
            {"fetched": 100, "service": 20, "outside": 5, "local": 3, "review": 12}, None
        )):
            self.app_module._run_background_refresh()
        status = self.client.get("/api/refresh/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["status"], "incomplete")
        self.assertIn("Some source results may be missing", status.json["summary"])
        self.assertIn("truncated", status.json["error"])
        self.assertIsNotNone(status.json["finished_at"])

    def test_refresh_feed_issues_flags_page_and_time_truncation(self):
        self.app_module.LAST_FEEDS.clear()
        self.app_module.LAST_FEEDS["Find a Tender"] = {
            "stopped": "time limit", "truncated_windows": 2
        }
        self.app_module.LAST_FEEDS["Contracts Finder"] = {
            "last_batch": 100, "full_end": True, "pagination_supported": True
        }
        self.app_module.LAST_FEEDS["Standstill UK RSS discovery"] = {
            "last_batch": 148, "known_feed_cap": 50, "pagination_supported": False
        }
        issues = self.app_module.refresh_feed_issues()
        self.assertEqual(len(issues), 2)
        self.assertIn("stopped early", issues[0])
        self.assertIn("possible page truncation", issues[1])

    def test_api_refresh_status_reports_success_and_summary(self):
        collector = self.app_module.collector

        with patch.object(self.app_module, "_start_refresh_thread", side_effect=self.app_module._run_background_refresh), \
             patch.object(collector, "fetch_fts", return_value=[feed_record("liv", "WA8 7AA")]), \
             patch.object(collector, "fetch_contracts_finder", return_value=[]), \
             patch.object(collector, "fetch_bearings", return_value=[]), \
             patch.object(collector, "fetch_standstill_northwest", return_value=[]):
            started = self.client.post("/api/refresh")
            status = self.client.get("/api/refresh/status")
        self.assertEqual(started.status_code, 202)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json["status"], "completed")
        self.assertIn("1 confirmed in your area", status.json["summary"])


class FeedDiagnosticsTests(AppTestCase):
    def test_dashboard_shows_what_each_feed_returned(self):
        collector = self.app_module.collector

        def fake_fts(days, pages, limit, stats):
            stats.update({"pages": 1, "records": 100, "last_batch": 100, "more": False,
                          "error": "", "stopped": "", "pagination_supported": True, "full_end": True, "first_date": "2026-10-11",
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
        with (patch.object(tenderscout, "_fts_windows", return_value=[(tenderscout.datetime(2026, 10, 10, tzinfo=tenderscout.timezone.utc), tenderscout.datetime(2026, 10, 11, tzinfo=tenderscout.timezone.utc))]), patch.object(tenderscout, "get_json", return_value=payload)):
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
        with (patch.object(tenderscout, "_fts_windows", return_value=[(tenderscout.datetime(2026, 10, 10, tzinfo=tenderscout.timezone.utc), tenderscout.datetime(2026, 10, 11, tzinfo=tenderscout.timezone.utc))]), patch.object(tenderscout, "get_json", side_effect=fake_get), patch.object(tenderscout.time, "sleep")):
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
