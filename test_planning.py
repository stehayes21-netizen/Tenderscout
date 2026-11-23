"""Offline tests for planning-application leads. Records are real PlanIt entries (November 2026), trimmed."""
import unittest
from datetime import date

import planning

REAL = [
    {"name": "StHelens/P/2026/0539/HHFP", "uid": "P/2026/0539/HHFP", "area_name": "St Helens", "start_date": "2026-10-13", "decided_date": None, "app_state": "Undecided", "app_size": "Small", "app_type": "Full", "address": "25 Parkside Avenue Clock Face St Helens WA9 4DT", "postcode": "WA9 4DT", "description": "Single storey side extension.", "link": "https://www.planit.org.uk/planapplic/StHelens/P/2026/0539/HHFP/", "url": "https://publicaccess.sthelens.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=TMUBV0PELKU00"},
    {"name": "StHelens/P/2026/0576/TPO", "uid": "P/2026/0576/TPO", "area_name": "St Helens", "start_date": "2026-11-09", "decided_date": None, "app_state": "Undecided", "app_size": "Small", "app_type": "Trees", "address": "31 View Road Rainhill St Helens L35 0LF", "postcode": "L35 0LF", "description": "Works to 2no beech trees under TPO.", "link": "https://www.planit.org.uk/planapplic/StHelens/P/2026/0576/TPO/", "url": "https://publicaccess.sthelens.gov.uk/online-applications/applicationDetails.do?keyVal=TO4XL6PELXI00&activeTab=summary"},
    {"name": "StHelens/P/2026/0567/FUL", "uid": "P/2026/0567/FUL", "area_name": "St Helens", "start_date": "2026-10-30", "decided_date": None, "app_state": "Undecided", "app_size": "Small", "app_type": "Full", "address": "Land At Rear Of 71 Litherland Crescent St Helens WA11 9AJ", "postcode": "WA11 9AJ", "description": "Demolition of existing garage and construction of 1no bungalow with associated landscaping.", "link": "https://www.planit.org.uk/planapplic/StHelens/P/2026/0567/FUL/", "url": "https://publicaccess.sthelens.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=TNQB9YPE04I00"},
    {"name": "StHelens/P/2026/0593/FUL", "uid": "P/2026/0593/FUL", "area_name": "St Helens", "start_date": "2026-11-12", "decided_date": None, "app_state": "Undecided", "app_size": "Large", "app_type": "Full", "address": "Land At Ullswater Avenue, St Helens", "postcode": None, "description": "Erection of 33no. dwellings (Class C3), construction of new vehicle access points, associated landscaping and infrastructure works", "link": "https://www.planit.org.uk/planapplic/StHelens/P/2026/0593/FUL/", "url": "https://publicaccess.sthelens.gov.uk/online-applications/applicationDetails.do?keyVal=TOE9T3PELZW00&activeTab=summary"},
    {"name": "StHelens/A/2026/0037/NMA", "uid": "A/2026/0037/NMA", "area_name": "St Helens", "start_date": "2026-10-28", "decided_date": "2026-11-20", "app_state": "Permitted", "app_size": "Small", "app_type": "Amendment", "address": "90 Park Road South Newton Le Willows St Helens WA12 8PL", "postcode": "WA12 8PL", "description": "Non-material amendment to approved extension on application P/2026/0222/HHFP, to alter the window and doors at rear", "link": "https://www.planit.org.uk/planapplic/StHelens/A/2026/0037/NMA/", "url": ""},
    {"name": "StHelens/C/2026/0164/CON", "uid": "C/2026/0164/CON", "area_name": "St Helens", "start_date": "2026-11-19", "decided_date": None, "app_state": "Undecided", "app_size": "Small", "app_type": "Conditions", "address": "Land West Of Mill Lane Newton Le Willows St Helens WA12 8DE", "postcode": "WA12 8DE", "description": "Discharge of condition 12 (Tree protection) on approval P/2023/0619/FUL.", "link": "https://www.planit.org.uk/planapplic/StHelens/C/2026/0164/CON/", "url": ""},
    {"name": "StHelens/P/2026/0559/CLP", "uid": "P/2026/0559/CLP", "area_name": "St Helens", "start_date": "2026-10-19", "decided_date": "2026-11-20", "app_state": "Rejected", "app_size": "Small", "app_type": "Outline", "address": "FC St Helens Windleshaw Road Dentons Green St Helens WA10 6TE", "postcode": "WA10 6TE", "description": "Certificate of lawfulness for proposed replacement of current grass pitch with artificial grass pitch.", "link": "https://www.planit.org.uk/planapplic/StHelens/P/2026/0559/CLP/", "url": ""},
    {"name": "StHelens/P/2026/0562/ADC", "uid": "P/2026/0562/ADC", "area_name": "St Helens", "start_date": "2026-10-15", "decided_date": None, "app_state": "Undecided", "app_size": "Small", "app_type": "Advertising", "address": "Tesco Stores James Roby Way St Helens WA9 5DT", "postcode": "WA9 5DT", "description": "Consent to display gym signage at premises.", "link": "https://www.planit.org.uk/planapplic/StHelens/P/2026/0562/ADC/", "url": ""},
    {"name": "StHelens/P/2026/0599/FP3", "uid": "P/2026/0599/FP3", "area_name": "St Helens", "start_date": "2026-11-18", "decided_date": None, "app_state": "Undecided", "app_size": "Small", "app_type": "Full", "address": "Boathouse & Cafe Taylor Park Grosvenor Road St Helens", "postcode": None, "description": "Works to existing boathouse, including replacement of timber windows with UPVC, renewal of dormers, installation of new aluminium doors and metal roller shutters, removal of existing turnstile, replacement fencing, and creation of rear lakeside seating area, including new glazed balustrade.", "link": "https://www.planit.org.uk/planapplic/StHelens/P/2026/0599/FP3/", "url": ""},
    {"name": "Knowsley/26/00543/KMBC1", "uid": "26/00543/KMBC1", "area_name": "Knowsley", "start_date": "2026-11-12", "decided_date": None, "app_state": "Undecided", "app_size": "Small", "app_type": "Other", "address": "60 St Chads Parade Kirkby Town Centre Knowsley L32 8UG", "postcode": "L32 8UG", "description": "EXTERNAL ALTERATIONS TO EXISTING COMMERCIAL UNITS, INCLUDING INCREASED SHOPFONT GLAZING, REMOVAL OF EXISTING CONCRETE CANOPY, REPLACEMENT OF EXISTING FIRST FLOOR WINDOWS OF UNITS 60 AND 62 TOGETHER WITH INTERNAL ALTERATIONS AND ASSOCIATED WORKS.", "link": "https://www.planit.org.uk/planapplic/Knowsley/26/00543/KMBC1/", "url": ""},
    {"name": "Sefton/DC/2026/01869", "uid": "DC/2026/01869", "area_name": "Sefton", "start_date": "2026-10-30", "decided_date": None, "app_state": "Undecided", "app_size": None, "app_type": None, "address": "152 Sherwoods Lane Liverpool L10 1AB", "postcode": "L10 1AB", "description": "Neighbouring Authority Consultation from Liverpool City Council to erect 2 no. dormer extension to side with associated loft conversion", "link": "https://www.planit.org.uk/planapplic/Sefton/DC/2026/01869/", "url": ""},
    {"name": "StHelens/P/2026/0572/O2DPA", "uid": "P/2026/0572/O2DPA", "area_name": "St Helens", "start_date": "2026-11-04", "decided_date": None, "app_state": "Undecided", "app_size": "Small", "app_type": "Outline", "address": "24 - 26 Ormskirk Street St Helens WA10 1BJ", "postcode": "WA10 1BJ", "description": "Prior approval for change of use from offices (E) to 4no apartments (C3) at ground floor.", "link": "https://www.planit.org.uk/planapplic/StHelens/P/2026/0572/O2DPA/", "url": ""},
]


class ClassifyTests(unittest.TestCase):
    def leads(self):
        return [l for l in (planning.normalise(r, "2026-11-23T00:00:00Z") for r in REAL) if l]

    def test_paperwork_rejected_and_adverts_are_dropped(self):
        names = {l["name"] for l in self.leads()}
        for dropped in ["StHelens/A/2026/0037/NMA", "StHelens/C/2026/0164/CON", "StHelens/P/2026/0559/CLP",
                        "StHelens/P/2026/0562/ADC", "Sefton/DC/2026/01869"]:
            self.assertNotIn(dropped, names)
        self.assertEqual(len(names), 7)

    def test_trades_are_labelled_from_the_description(self):
        by = {l["name"]: l["trades"] for l in self.leads()}
        self.assertTrue(by["StHelens/P/2026/0539/HHFP"].startswith("Extensions and building work"))
        self.assertEqual(by["StHelens/P/2026/0576/TPO"], "Trees and landscaping")
        self.assertIn("New homes and conversions", by["StHelens/P/2026/0567/FUL"])
        self.assertIn("Demolition and clearance", by["StHelens/P/2026/0567/FUL"])
        self.assertIn("Builders' cleans", by["StHelens/P/2026/0593/FUL"])
        self.assertIn("Windows and doors", by["StHelens/P/2026/0599/FP3"])
        self.assertIn("Roofing", by["StHelens/P/2026/0599/FP3"])
        self.assertIn("Shopfronts and commercial fit-out", by["Knowsley/26/00543/KMBC1"])
        self.assertIn("New homes and conversions", by["StHelens/P/2026/0572/O2DPA"])

    def test_household_flag_and_links(self):
        lead = next(l for l in self.leads() if l["name"] == "StHelens/P/2026/0539/HHFP")
        self.assertEqual(lead["household"], 1)
        self.assertTrue(lead["council_url"].startswith("https://publicaccess.sthelens.gov.uk/"))
        self.assertEqual(lead["postcode"], "WA9 4DT")
        big = next(l for l in self.leads() if l["name"] == "StHelens/P/2026/0593/FUL")
        self.assertEqual(big["household"], 0)

    def test_status_labels(self):
        self.assertTrue(planning.status_label("Permitted").startswith("Approved"))
        self.assertTrue(planning.status_label("Undecided").startswith("Awaiting decision"))


class FetchTests(unittest.TestCase):
    def test_fetch_all_pages_and_reports_each_area(self):
        calls = []

        def getter(params, timeout):
            calls.append((params["auth"], params["page"]))
            if params["auth"] == "Wirral":
                raise OSError("timed out")
            if params["auth"] == "St Helens":
                page = params["page"]
                recs = REAL[:6] if page == 1 else REAL[6:]
                return {"records": recs, "total": len(REAL), "from": 0 if page == 1 else 6, "to": 5 if page == 1 else len(REAL) - 1}
            return {"records": [], "total": 0, "from": None, "to": None}

        waits = []
        leads, infos = planning.fetch_all(areas=["St Helens", "Wirral", "Liverpool"], getter=getter, sleep=waits.append)
        # One minute between requests, none before the first: 4 requests -> 3 waits.
        self.assertEqual(waits, [60, 60, 60])
        self.assertIn(("St Helens", 2), calls)
        self.assertEqual(len(leads), 7)
        info = {i["area"]: i for i in infos}
        self.assertEqual(info["St Helens"]["kept"], 7)
        self.assertEqual(info["St Helens"]["newest"], "2026-11-19")
        self.assertIn("timed out", info["Wirral"]["error"])
        self.assertTrue(planning.area_is_stale(info["Liverpool"], today=date(2026, 11, 23)))
        self.assertFalse(planning.area_is_stale(info["St Helens"], today=date(2026, 11, 23)))
        self.assertFalse(planning.area_is_stale(info["Wirral"], today=date(2026, 11, 23)))

    def test_old_newest_date_is_stale(self):
        self.assertTrue(planning.area_is_stale({"newest": "2026-07-08", "error": ""}, today=date(2026, 11, 23)))


if __name__ == "__main__":
    unittest.main()


class PlanningPageTests(unittest.TestCase):
    def setUp(self):
        import base64, importlib, os, tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["TENDERSCOUT_DB"] = str(Path(self.tmp.name) / "test.db")
        os.environ.pop("DATABASE_URL", None)
        os.environ["DASHBOARD_USER"] = "u"
        os.environ["DASHBOARD_PASSWORD"] = "p"
        import app
        self.app = importlib.reload(app)
        self.client = self.app.app.test_client()
        self.client.environ_base["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(b"u:p").decode()

    def tearDown(self):
        import os
        self.tmp.cleanup()
        for name in ("TENDERSCOUT_DB", "DASHBOARD_USER", "DASHBOARD_PASSWORD"):
            os.environ.pop(name, None)

    def getter(self, params, timeout):
        if params["auth"] == "St Helens":
            return {"records": REAL, "total": len(REAL), "from": 0, "to": len(REAL) - 1}
        if params["auth"] == "Knowsley":
            return {"records": [REAL[9]], "total": 1, "from": 0, "to": 0}
        return {"records": [], "total": 0, "from": None, "to": None}

    def test_empty_page_invites_a_check(self):
        page = self.client.get("/planning").get_data(as_text=True)
        self.assertIn("Check for new planning applications", page)
        self.assertIn("No planning leads saved yet", page)

    def test_refresh_saves_leads_and_page_filters_them(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 23, tzinfo=timezone.utc)
        leads, infos = self.app.refresh_planning_leads(getter=self.getter, now=now, sleep=lambda s: None)
        self.assertEqual(len(leads), 7)  # the Knowsley record is in both samples: saved once
        # Running twice updates rather than duplicates.
        self.app.refresh_planning_leads(getter=self.getter, now=now, sleep=lambda s: None)
        page = self.client.get("/planning").get_data(as_text=True)
        self.assertIn("7 leads saved", page)
        self.assertIn("Single storey side extension.", page)
        self.assertIn("Works to 2no beech trees under TPO.", page)
        self.assertNotIn("Discharge of condition", page)
        self.assertIn("not updating on PlanIt", page)  # Liverpool returned nothing
        trees = self.client.get("/planning?trade=Trees+and+landscaping").get_data(as_text=True)
        self.assertIn("beech trees", trees)
        self.assertNotIn("Single storey side extension.", trees)
        knowsley = self.client.get("/planning?area=Knowsley").get_data(as_text=True)
        self.assertIn("ST CHADS PARADE", knowsley.upper())
        self.assertNotIn("Parkside Avenue", knowsley)
        self.assertIn("letter or leaflet only", page)

    def test_planning_page_requires_login(self):
        self.client.environ_base.pop("HTTP_AUTHORIZATION")
        self.assertEqual(self.client.get("/planning").status_code, 401)


class PoliteUseTests(unittest.TestCase):
    def test_contact_email_is_required_and_sent(self):
        import os
        os.environ.pop("PLANIT_CONTACT_EMAIL", None)
        with self.assertRaises(planning.PlanningConfigError):
            planning.user_agent()
        os.environ["PLANIT_CONTACT_EMAIL"] = "someone@example.com"
        try:
            self.assertIn("someone@example.com", planning.user_agent())
        finally:
            os.environ.pop("PLANIT_CONTACT_EMAIL", None)

    def test_quiet_hours_follow_uk_time(self):
        from datetime import datetime, timezone
        self.assertTrue(planning.in_quiet_hours(datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc)))
        self.assertTrue(planning.in_quiet_hours(datetime(2026, 11, 23, 5, 30, tzinfo=timezone.utc)))
        self.assertFalse(planning.in_quiet_hours(datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)))
        # British Summer Time: 17:30 UTC is 18:30 in London.
        self.assertTrue(planning.in_quiet_hours(datetime(2026, 7, 1, 17, 30, tzinfo=timezone.utc)))

    def test_retry_after_is_honoured_once(self):
        import io
        from unittest import mock
        from urllib.error import HTTPError
        import os
        os.environ["PLANIT_CONTACT_EMAIL"] = "someone@example.com"
        calls, waits = [], []

        def fake_urlopen(req, timeout):
            calls.append(req.get_header("User-agent"))
            if len(calls) == 1:
                raise HTTPError(req.full_url, 429, "Too Many", {"Retry-After": "120"}, io.BytesIO(b""))
            return io.BytesIO(b'{"records": [], "total": 0}')

        try:
            with mock.patch.object(planning, "urlopen", fake_urlopen):
                data = planning._get({"auth": "Sefton"}, 5, sleep=waits.append)
        finally:
            os.environ.pop("PLANIT_CONTACT_EMAIL", None)
        self.assertEqual(data["total"], 0)
        self.assertEqual(waits, [120])
        self.assertIn("someone@example.com", calls[0])


class PlanningScheduleTests(PlanningPageTests):
    def test_button_refuses_in_daytime_and_without_email(self):
        import os
        from datetime import datetime, timezone
        noon = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        evening = datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc)
        os.environ.pop("PLANIT_CONTACT_EMAIL", None)
        self.assertEqual(self.app.start_planning_refresh(now=evening), "config")
        os.environ["PLANIT_CONTACT_EMAIL"] = "someone@example.com"
        try:
            self.assertEqual(self.app.start_planning_refresh(now=noon), "daytime")
            page = self.client.get("/planning?check=daytime").get_data(as_text=True)
            self.assertIn("between 6pm and 6am", page)
        finally:
            os.environ.pop("PLANIT_CONTACT_EMAIL", None)

    def test_automatic_check_runs_at_most_once_a_day(self):
        import os
        from datetime import datetime, timedelta, timezone
        from unittest import mock
        # Real "now" so the page's 30-minute running window is current; quiet hours forced on.
        evening = datetime.now(timezone.utc)
        os.environ["PLANIT_CONTACT_EMAIL"] = "someone@example.com"
        quiet = mock.patch.object(planning, "in_quiet_hours", return_value=True)
        quiet.start()
        self.addCleanup(quiet.stop)
        try:
            with mock.patch.object(self.app.threading, "Thread") as thread:
                self.assertEqual(self.app.start_planning_refresh(now=evening, automatic=True), "started")
                self.assertEqual(self.app.start_planning_refresh(now=evening, automatic=True), "running")
                thread.return_value.start.assert_called_once()
            page = self.client.get("/planning").get_data(as_text=True)
            self.assertIn("Checking PlanIt now", page)
            # Pretend it finished an hour later; the next automatic check that night is skipped.
            db = self.app.connect_db()
            self.app._set_planning_run(db, status="done", ran_at=(evening + timedelta(hours=1)).isoformat())
            db.close()
            with mock.patch.object(self.app.threading, "Thread"):
                self.assertEqual(self.app.start_planning_refresh(now=evening + timedelta(hours=5), automatic=True), "recent")
                self.assertEqual(self.app.start_planning_refresh(now=evening + timedelta(hours=23), automatic=True), "started")
        finally:
            os.environ.pop("PLANIT_CONTACT_EMAIL", None)


class ExteriorTradeTests(unittest.TestCase):
    """Roofline, gutters and exterior work (gutter, fascia and soffit firms, roof cleaners and repairers)."""

    def test_exterior_planning_jobs_are_labelled(self):
        cases = [
            "Replacement of existing timber fascias, soffits and guttering with uPVC",
            "Re-roofing of existing dwelling with replacement slates and new rainwater goods",
            "Application for Non Material Amendment to planning permission 26/00024/FUL for the proposed rendering of existing property",
            "External wall insulation and render to front and side elevations",
        ]
        for text in cases:
            self.assertIn("Roofline, gutters and exterior", planning.classify(text), text)
        self.assertNotIn("Roofline, gutters and exterior", planning.classify("Single storey side extension."))

    def test_exterior_tenders_are_labelled(self):
        import tenderscout
        self.assertEqual(tenderscout.classify_trade_category("Gutter clearing and downpipe maintenance across council housing"), "Gutter cleaning")
        self.assertEqual(tenderscout.classify_trade_category("Replacement of fascia and soffit boards to 40 properties"), "Roofline, fascias and soffits")
        self.assertEqual(tenderscout.classify_trade_category("Re-roofing programme for sheltered housing blocks"), "Roofing and roof repairs")


class KeepAwakeTests(PlanningPageTests):
    def test_killed_check_frees_the_button_after_three_minutes(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 23, 19, 26, tzinfo=timezone.utc)
        db = self.app.connect_db()
        self.app._set_planning_run(db, status="running", started_at=now.isoformat(), heartbeat_at=now.isoformat())
        row = self.app._planning_run(db)
        db.close()
        self.assertTrue(self.app.planning_running(row, now + timedelta(minutes=2)))
        self.assertFalse(self.app.planning_running(row, now + timedelta(minutes=4)))

    def test_waiting_between_requests_keeps_the_check_alive(self):
        import os
        from unittest import mock
        pings, sleeps = [], []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b"ok"

        def fake_urlopen(req, timeout):
            pings.append(req.full_url)
            return Resp()

        with mock.patch.dict(os.environ, {"RENDER_EXTERNAL_URL": "https://tenderscout-1qnz.onrender.com"}), \
             mock.patch.object(self.app, "urlopen", fake_urlopen), \
             mock.patch("time.sleep", sleeps.append):
            self.app._keepalive_sleep(60)
        self.assertEqual(sleeps, [30, 30])
        self.assertEqual(pings, ["https://tenderscout-1qnz.onrender.com/health"] * 2)
        db = self.app.connect_db()
        self.assertTrue(self.app._planning_run(db)["heartbeat_at"])
        db.close()

    def test_no_render_address_means_no_ping(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"RENDER_EXTERNAL_URL": ""}), \
             mock.patch.object(self.app, "urlopen", side_effect=AssertionError("should not ping")):
            self.app._ping_self()


class PushButtonTests(PlanningPageTests):
    def test_button_is_a_push_button_and_greys_out_while_running(self):
        page = self.client.get("/planning").get_data(as_text=True)
        self.assertIn('class="push" id="checkbtn"', page)
        from datetime import datetime, timezone
        db = self.app.connect_db()
        now = datetime.now(timezone.utc).isoformat()
        self.app._set_planning_run(db, status="running", started_at=now, heartbeat_at=now)
        db.close()
        page = self.client.get("/planning").get_data(as_text=True)
        self.assertIn("Checking now", page)
        self.assertIn('class="push" disabled', page)

    def test_all_councils_refused_is_explained(self):
        import json
        db = self.app.connect_db()
        infos = [{"area": a, "error": "HTTP Error 503: Service Temporarily Unavailable", "kept": 0, "total": 0, "newest": ""}
                 for a in ("Sefton", "Knowsley")]
        self.app._set_planning_run(db, status="done", infos_json=json.dumps(infos), ran_at="2026-11-23T19:45:00+00:00")
        db.close()
        page = self.client.get("/planning").get_data(as_text=True)
        self.assertIn("PlanIt turned down every request", page)
