import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import tenderscout


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class FindATenderPaginationTests(unittest.TestCase):
    def test_hosted_collector_allows_six_pages_per_window(self):
        windows = [
            (dt("2026-10-21T00:00:00"), dt("2026-10-28T00:00:00")),
            (dt("2026-10-14T00:00:00"), dt("2026-10-21T00:00:00")),
            (dt("2026-10-07T00:00:00"), dt("2026-10-14T00:00:00")),
        ]
        calls = []

        def fake_page_through(base_url, params, source, max_pages, started, max_seconds, stats):
            calls.append(max_pages)
            return []

        with patch.object(tenderscout, "_fts_windows", return_value=windows), \
             patch.object(tenderscout, "_page_through", side_effect=fake_page_through), \
             patch.object(tenderscout.time, "sleep"):
            result = tenderscout.fetch_fts(days=21, max_pages=10, max_seconds=150)

        self.assertEqual(result, [])
        self.assertEqual(calls, [6, 6, 6])

    def test_page_limit_marks_window_as_truncated(self):
        stats = tenderscout._start_stats({})
        first = {"releases": [{"id": "a"}], "links": {"next": "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?cursor=next"}}
        with patch.object(tenderscout, "get_json", return_value=first), \
             patch.object(tenderscout, "extract_records", return_value=[{"title": "Example"}]), \
             patch.object(tenderscout.time, "sleep"):
            rows = tenderscout._page_through(
                tenderscout.FTS_URL, {"limit": "100"}, "Find a Tender", 1,
                tenderscout.time.monotonic(), None, stats
            )
        self.assertEqual(len(rows), 1)
        self.assertTrue(stats["more"])
        self.assertEqual(stats["truncated_windows"], 1)

    def test_cli_audit_page_limit_is_respected_without_hosted_time_budget(self):
        windows = [
            (dt("2026-10-21T00:00:00"), dt("2026-10-28T00:00:00")),
            (dt("2026-10-14T00:00:00"), dt("2026-10-21T00:00:00")),
        ]
        calls = []

        def fake_page_through(base_url, params, source, max_pages, started, max_seconds, stats):
            calls.append(max_pages)
            return []

        with patch.object(tenderscout, "_fts_windows", return_value=windows) as window_mock, \
             patch.object(tenderscout, "_page_through", side_effect=fake_page_through), \
             patch.object(tenderscout.time, "sleep"):
            tenderscout.fetch_fts(days=14, max_pages=10)

        self.assertEqual(calls, [10, 10])
        window_mock.assert_called_once_with(14)


class FindATenderWindowStrategyTests(unittest.TestCase):
    def test_recent_lookback_uses_daily_windows_then_weekly_history(self):
        windows = tenderscout._fts_windows(24, recent_days=10)
        self.assertEqual(len(windows), 12)  # 10 daily windows plus 2 older weekly windows
        self.assertEqual(windows[0][1] - windows[0][0], tenderscout.timedelta(days=1))
        self.assertEqual(windows[9][1] - windows[9][0], tenderscout.timedelta(days=1))
        self.assertEqual(windows[10][1], windows[9][0])
        self.assertEqual(windows[10][1] - windows[10][0], tenderscout.timedelta(days=7))


class FindATenderDirectLookupTests(unittest.TestCase):
    def test_known_notice_uses_official_id_endpoint(self):
        record = {"title": "Window Cleaning Services - Wirral", "notice_url": "https://www.find-tender.service.gov.uk/Notice/098300-2026"}
        with patch.object(tenderscout, "get_json", return_value={"releases": [{"id": "098300-2026", "tender": {"title": "Window Cleaning Services - Wirral"}}]}) as get_json:
            with patch.object(tenderscout, "extract_records", return_value=[record]):
                result = tenderscout.fetch_fts_notice("098300-2026")
        self.assertEqual(result, [record])
        get_json.assert_called_once_with("https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages/098300-2026")

    def test_rejects_invalid_notice_id(self):
        with self.assertRaises(ValueError):
            tenderscout.fetch_fts_notice("../098300-2026")


class SourceCoverageDiagnosticsTests(unittest.TestCase):
    def test_full_sized_page_without_next_cursor_is_not_called_truncated(self):
        stats = tenderscout._start_stats({})
        batch = [{"id": str(i)} for i in range(100)]
        payload = {"releases": [{}]}
        with patch.object(tenderscout, "get_json", return_value=payload), \
             patch.object(tenderscout, "extract_records", return_value=batch):
            rows = tenderscout._page_through(
                tenderscout.FTS_URL, {"limit": "100"}, "Find a Tender", 2,
                tenderscout.time.monotonic(), None, stats
            )
        self.assertEqual(len(rows), 100)
        self.assertFalse(stats["more"])
        self.assertFalse(stats["full_end"])
        self.assertEqual(stats["truncated_windows"], 0)

    def test_empty_page_with_next_cursor_continues_pagination(self):
        stats = tenderscout._start_stats({})
        payloads = [
            {"releases": [], "links": {"next": "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?cursor=next"}},
            {"releases": [{}]},
        ]
        batches = [[], [{"id": "later-page"}]]
        with patch.object(tenderscout, "get_json", side_effect=payloads) as get_json, \
             patch.object(tenderscout, "extract_records", side_effect=batches), \
             patch.object(tenderscout.time, "sleep"):
            rows = tenderscout._page_through(
                tenderscout.FTS_URL, {"limit": "100"}, "Find a Tender", 2,
                tenderscout.time.monotonic(), None, stats
            )
        self.assertEqual(rows, [{"id": "later-page"}])
        self.assertEqual(get_json.call_count, 2)
        self.assertFalse(stats["more"])

    def test_bearings_200_item_limit_is_reported_as_possible_truncation(self):
        stats = {}
        data = [
            {"kind": "tender", "title": f"Opportunity {i}",
             "url": f"https://example.com/opportunity/{i}"}
            for i in range(200)
        ]
        with patch.object(tenderscout, "get_json", return_value={"data": data}):
            rows = tenderscout.fetch_bearings(stats=stats)
        self.assertEqual(len(rows), 200)
        self.assertTrue(stats["more"])
        self.assertEqual(stats["truncated_windows"], 1)
        self.assertFalse(stats["full_end"])


if __name__ == "__main__":
    unittest.main()


class FindATenderRateLimitTests(unittest.TestCase):
    def test_429_waits_for_retry_after_before_one_retry(self):
        import io
        from unittest.mock import MagicMock
        error = __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages",
            429, "Too Many Requests", {"Retry-After": "120"}, io.BytesIO(b"rate limited")
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"releases": []}'
        with patch.object(tenderscout.urllib.request, "urlopen", side_effect=[error, response]) as opener, \
             patch.object(tenderscout.time, "sleep") as sleep:
            result = tenderscout.get_json(tenderscout.FTS_URL)
        self.assertEqual(result, {"releases": []})
        sleep.assert_called_once_with(120)
        self.assertEqual(opener.call_count, 2)

    def test_429_without_retry_after_uses_conservative_120_seconds(self):
        import io
        from unittest.mock import MagicMock
        error = __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages",
            429, "Too Many Requests", {}, io.BytesIO(b"rate limited")
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"releases": []}'
        with patch.object(tenderscout.urllib.request, "urlopen", side_effect=[error, response]), \
             patch.object(tenderscout.time, "sleep") as sleep:
            tenderscout.get_json(tenderscout.FTS_URL)
        sleep.assert_called_once_with(120)
