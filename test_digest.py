"""Offline tests for the weekly subscriber emails."""
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

import digest
import planning
from test_planning import REAL, PlanningPageTests

MONDAY_9AM = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)   # Monday
SUNDAY = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)


def sub(**kw):
    base = {"id": 1, "name": "Dave Jones", "business": "DJ Building", "email": "dave@example.com",
            "planning_trades": "Extensions and building work", "tender_trades": "", "areas": "",
            "active": 1, "last_sent_at": None}
    base.update(kw)
    return base


class DigestUnitTests(unittest.TestCase):
    def test_send_window_and_due(self):
        self.assertTrue(digest.is_send_window(MONDAY_9AM))
        self.assertFalse(digest.is_send_window(SUNDAY))
        self.assertFalse(digest.is_send_window(datetime(2026, 11, 23, 5, 0, tzinfo=timezone.utc)))
        self.assertTrue(digest.due(sub(), MONDAY_9AM))
        self.assertFalse(digest.due(sub(last_sent_at="2026-11-23T07:00:00+00:00"), MONDAY_9AM))
        self.assertTrue(digest.due(sub(last_sent_at="2026-11-16T07:00:00+00:00"), MONDAY_9AM))
        self.assertFalse(digest.due(sub(active=0), MONDAY_9AM))

    def test_matching_by_trade_and_area_with_approved_first(self):
        leads = [planning.normalise(r) for r in REAL]
        leads = [l for l in leads if l]
        leads[0]["app_state"] = "Permitted"
        leads[0]["decided_date"] = "2026-11-20"
        got = digest.match_planning(sub(), leads)
        self.assertTrue(all("Extensions and building work" in l["trades"] for l in got))
        self.assertEqual(got[0]["app_state"], "Permitted")
        trees_sefton = digest.match_planning(sub(planning_trades="Trees and landscaping", areas="Sefton"), leads)
        self.assertEqual(trees_sefton, [])
        trees = digest.match_planning(sub(planning_trades="Trees and landscaping"), leads)
        self.assertTrue(any("beech" in l["description"] for l in trees))

    def test_compose_has_leads_privacy_note_and_nothing_when_empty(self):
        leads = [l for l in (planning.normalise(r) for r in REAL) if l][:2]
        tenders = [{"title": "Grounds maintenance", "buyer": "Sefton Council", "deadline": "2026-12-18T12:00:00Z",
                    "notice_url": "https://www.find-tender.service.gov.uk/Notice/1", "trade_category": "Grounds maintenance and gardening"}]
        subject, text, body = digest.compose(sub(), leads, tenders, planning.status_label, MONDAY_9AM)
        self.assertEqual(subject, "3 new job leads in Merseyside this week")
        self.assertIn("Hi Dave,", text)
        self.assertIn("letter or leaflet only", text)
        self.assertIn("Open the tender notice", body)
        self.assertIsNone(digest.compose(sub(), [], [], planning.status_label))

    def test_send_uses_starttls_and_login(self):
        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                sent["host"] = (host, port)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self): sent["tls"] = True
            def login(self, u, p): sent["login"] = u
            def send_message(self, msg): sent["msg"] = msg

        settings = {"host": "smtp.gmail.com", "port": 587, "user": "me@gmail.com", "password": "x",
                    "from": "me@gmail.com", "reply_to": "", "name": "Merseyside Trade Leads"}
        digest.send("dave@example.com", "Hello", "text", "<p>html</p>", settings, smtp_factory=FakeSMTP)
        self.assertTrue(sent["tls"])
        self.assertEqual(sent["login"], "me@gmail.com")
        self.assertEqual(sent["msg"]["To"], "dave@example.com")

    def test_missing_smtp_settings_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"SMTP_USER": "", "SMTP_PASSWORD": ""}):
            with self.assertRaises(digest.DigestConfigError):
                digest.smtp_settings()


class SubscriberPageTests(PlanningPageTests):
    def seed(self):
        self.app.refresh_planning_leads(getter=self.getter, now=MONDAY_9AM, sleep=lambda s: None)
        db = self.app.connect_db()
        db.execute(
            "INSERT INTO tenders (title,buyer,notice_url,region_match,opportunity_stage,trade_category,deadline,published_date) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("Grounds maintenance contract", "Sefton Council", "https://www.find-tender.service.gov.uk/Notice/9",
             "LOCAL", "Open for bids", "Grounds maintenance and gardening", "2099-01-01", "2026-11-20"),
        )
        db.commit()
        db.close()

    def add(self, **form):
        data = {"name": "Dave Jones", "business": "DJ Building", "email": "dave@example.com",
                "planning_trades": ["Extensions and building work"], "tender_trades": ["Grounds maintenance and gardening"]}
        data.update(form)
        return self.client.post("/subscribers", data=data)

    def test_add_preview_and_validation(self):
        self.seed()
        page = self.add().get_data(as_text=True)
        self.assertIn("Subscriber added", page)
        self.assertIn("Dave Jones", page)
        bad = self.add(email="not-an-email").get_data(as_text=True)
        self.assertIn("valid email", bad)
        # The preview uses today's date, so make the sample data current.
        today = datetime.now(timezone.utc).date().isoformat()
        db = self.app.connect_db()
        db.execute("UPDATE planning_leads SET start_date = ?", (today,))
        db.execute("UPDATE tenders SET published_date = ?", (today,))
        db.commit()
        db.close()
        preview = self.client.get("/subscribers/1/preview").get_data(as_text=True)
        self.assertIn("Single storey side extension.", preview)
        self.assertIn("Grounds maintenance contract", preview)

    def test_weekly_run_sends_once_and_only_on_monday(self):
        self.seed()
        self.add()
        outbox = []
        sender = lambda to, subject, text, body: outbox.append((to, subject))
        self.assertIn("skipped", self.app.send_weekly_digests(now=SUNDAY, sender=sender))
        # Leads in the fixture are from Oct/Nov 2026, so "now" must be near then for the 14-day window.
        result = self.app.send_weekly_digests(now=MONDAY_9AM, sender=sender)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(outbox[0][0], "dave@example.com")
        again = self.app.send_weekly_digests(now=datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc), sender=sender)
        self.assertEqual(again["sent"], 0)
        self.assertEqual(len(outbox), 1)

    def test_paused_subscribers_get_nothing_and_send_now_without_smtp_explains(self):
        self.seed()
        self.add()
        self.client.post("/subscribers/1/toggle")
        outbox = []
        self.app.send_weekly_digests(now=MONDAY_9AM, sender=lambda *a: outbox.append(a))
        self.assertEqual(outbox, [])
        with mock.patch.dict(os.environ, {"SMTP_USER": "", "SMTP_PASSWORD": ""}):
            r = self.client.post("/subscribers/1/send", follow_redirects=True)
        self.assertIn("isn&#x27;t set up yet", r.get_data(as_text=True).replace("isn't", "isn&#x27;t"))


if __name__ == "__main__":
    unittest.main()
