import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import tenderscout


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class FindATenderPaginationTests(unittest.TestCase):
    def test_hosted_collector_uses_configured_page_limit_per_window(self):
        windows = [
            (dt("2026-10-21T00:00:00"), dt("2026-10-28T00:00:00")),
            (dt("2026-10-14T00:00:00"), dt("2026-10-21T00:00:00")),
            (dt("2026-10-07T00:00:00"), dt("2026-10-14T00:00:00")),
        ]
        calls = []

        def fake_page_through(base_url, params, source, max_pages, started, max_seconds, stats, **kwargs):
            calls.append(max_pages)
            return []

        with (
            patch.object(tenderscout, "_fts_windows", return_value=windows),
            patch.object(tenderscout, "_page_through", side_effect=fake_page_through),
            patch.object(tenderscout.time, "sleep"),
        ):
            result = tenderscout.fetch_fts(days=21, max_pages=10, max_seconds=150)

        self.assertEqual(result, [])
        self.assertEqual(calls, [10, 10, 10])

    def test_resumed_window_keeps_prior_records_and_uses_saved_cursor(self):
        stats = tenderscout._start_stats({})
        prior = [{"id": "page-one"}]
        payload = {
            "releases": [{}],
            "links": {"next": "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages?cursor=last"},
        }
        batches = [[{"id": "page-two"}]]
        saved = []

        with (patch.object(tenderscout, "get_json", return_value=payload) as get_json,
              patch.object(tenderscout, "extract_records", side_effect=batches),
              patch.object(tenderscout.time, "sleep")):
            rows = tenderscout._page_through(
                tenderscout.FTS_URL, {"limit": "100"}, "Find a Tender", 1,
                tenderscout.time.monotonic(), None, stats,
                initial_cursor="resume-token", initial_results=prior,
                on_page=lambda records, cursor, complete: saved.append(
                    (list(records), cursor, complete)
                ),
            )

        self.assertEqual(rows, prior + batches[0])
        self.assertIn("cursor=resume-token", get_json.call_args.args[0])
        self.assertEqual(saved[0][0], prior + batches[0])
        self.assertTrue(saved[0][1])
        self.assertFalse(saved[0][2])

    def test_recent_completed_windows_are_refetched_but_history_is_reused(self):
        windows = [
            (dt("2026-11-04T00:00:00"), dt("2026-11-05T00:00:00")),
            (dt("2026-11-03T00:00:00"), dt("2026-11-04T00:00:00")),
            (dt("2026-10-26T00:00:00"), dt("2026-11-02T00:00:00")),
        ]

        class Checkpoint:
            def __init__(self):
                self.loaded = []
                self.saved = []

            def load(self, key):
                self.loaded.append(key)
                return {"records": [{"id": key}], "cursor": None, "complete": True}

            def save(self, key, state):
                self.saved.append((key, state))

        checkpoint = Checkpoint()
        fetch_inputs = []

        def fake_page_through(base_url, params, source, max_pages, started, max_seconds,
                              stats, initial_cursor=None, initial_results=None, on_page=None):
            fetch_inputs.append((initial_cursor, list(initial_results or [])))
            if on_page:
                on_page([], None, True)
            return [{"id": "fresh-" + params["updatedFrom"]}]

        stats = tenderscout._start_stats({})
        with (
            patch.object(tenderscout, "_fts_windows", return_value=windows),
            patch.object(tenderscout, "_page_through", side_effect=fake_page_through) as page_through,
            patch.object(tenderscout.time, "sleep"),
        ):
            rows = tenderscout.fetch_fts(
                days=10, max_pages=10, checkpoint=checkpoint, stats=stats
            )

        self.assertEqual(
            {row["id"] for row in rows},
            {
                "fresh-2026-11-04T00:00:00",
                "fresh-2026-11-03T00:00:00",
                "2026-10-26T00:00:00|2026-11-02T00:00:00",
            },
        )
        self.assertEqual(page_through.call_count, 2)
        self.assertEqual(fetch_inputs, [(None, []), (None, [])])
        self.assertEqual(len(checkpoint.saved), 2)
        self.assertEqual(stats["records"], 1, "reused records must be included in feed totals")

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

        def fake_page_through(base_url, params, source, max_pages, started, max_seconds, stats, **kwargs):
            calls.append(max_pages)
            return []

        with patch.object(tenderscout, "_fts_windows", return_value=windows) as window_mock, \
             patch.object(tenderscout, "_page_through", side_effect=fake_page_through), \
             patch.object(tenderscout.time, "sleep"):
            tenderscout.fetch_fts(days=14, max_pages=10)

        self.assertEqual(calls, [10, 10])
        window_mock.assert_called_once_with(14)


class FindATenderWindowStrategyTests(unittest.TestCase):
    def test_all_fts_windows_are_six_hours_and_cover_requested_lookback(self):
        real_datetime = tenderscout.datetime

        class Thursday(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

        with patch.object(tenderscout, "datetime", Thursday):
            windows = tenderscout._fts_windows(24)

        self.assertEqual(len(windows), 96)
        self.assertEqual(windows[0], (dt("2026-11-05T06:00:00"), dt("2026-11-05T12:00:00")))
        self.assertEqual(windows[-1], (dt("2026-10-12T12:00:00"), dt("2026-10-12T18:00:00")))
        self.assertTrue(all(end - start == tenderscout.timedelta(hours=6) for start, end in windows))
        for newer, older in zip(windows, windows[1:]):
            self.assertEqual(newer[0], older[1], "six-hour windows must be contiguous")

    def test_historical_six_hour_windows_stay_fixed_across_adjacent_days(self):
        real_datetime = tenderscout.datetime

        class Wednesday(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)

        class Thursday(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

        with patch.object(tenderscout, "datetime", Wednesday):
            wed = tenderscout._fts_windows(60)
        with patch.object(tenderscout, "datetime", Thursday):
            thu = tenderscout._fts_windows(60)

        # Four new six-hour windows are added for the new day; overlapping
        # historical intervals keep the same checkpoint keys.
        self.assertEqual(wed[:-4], thu[4:])
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
        self.assertFalse(stats["more"])
        self.assertTrue(stats["possible_cap"])
        self.assertEqual(stats["truncated_windows"], 0)
        self.assertFalse(stats["full_end"])
        self.assertEqual(tenderscout._coverage_status(stats), "POSSIBLE TRUNCATION")


class ConsistentSourceCoverageSummaryTests(unittest.TestCase):
    def test_cursor_remaining_is_incomplete(self):
        stats = tenderscout._start_stats({})
        stats.update({"windows": 2, "pages": 6, "records": 600,
                      "more": True, "truncated_windows": 1})
        self.assertEqual(tenderscout._coverage_status(stats), "INCOMPLETE")

    def test_source_error_is_incomplete_even_after_partial_records(self):
        stats = tenderscout._start_stats({})
        stats.update({"pages": 2, "records": 120, "error": "HTTP 429"})
        self.assertEqual(tenderscout._coverage_status(stats), "INCOMPLETE")

    def test_partial_standstill_feed_failure_is_incomplete(self):
        stats = tenderscout._start_stats({})
        stats.update({"pages": 5, "windows": 7, "records": 80,
                      "partial_errors": "Construction CPV: timeout"})
        self.assertEqual(tenderscout._coverage_status(stats), "INCOMPLETE")

    def test_standstill_feed_cap_is_possible_truncation(self):
        stats = tenderscout._start_stats({})
        stats.update({"pages": 7, "windows": 7, "records": 350,
                      "full_end": True})
        self.assertEqual(tenderscout._coverage_status(stats), "POSSIBLE TRUNCATION")

    def test_known_feed_failure_takes_priority_over_possible_cap(self):
        stats = tenderscout._start_stats({})
        stats.update({"pages": 6, "windows": 7, "records": 300,
                      "full_end": True, "partial_errors": "One feed timed out"})
        self.assertEqual(tenderscout._coverage_status(stats), "INCOMPLETE")

    def test_clean_cursor_end_is_not_misreported_as_incomplete(self):
        stats = tenderscout._start_stats({})
        stats.update({"pages": 3, "windows": 2, "records": 175})
        self.assertEqual(
            tenderscout._coverage_status(stats),
            "FETCH COMPLETE (pagination ended or source exposes no cursor)",
        )


class FindATenderDailyCheckpointTests(unittest.TestCase):
    def test_six_hour_windows_remain_stable_across_adjacent_days(self):
        real_datetime = tenderscout.datetime

        class Wednesday(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)

        class Thursday(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

        with patch.object(tenderscout, "datetime", Wednesday):
            wed = tenderscout._fts_windows(60)
        with patch.object(tenderscout, "datetime", Thursday):
            thu = tenderscout._fts_windows(60)

        self.assertEqual(wed[:-4], thu[4:])

    def test_incomplete_six_hour_window_resumes_with_saved_cursor_and_records(self):
        real_datetime = tenderscout.datetime

        class Thursday(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

        unfinished_key = "2026-11-05T06:00:00|2026-11-05T12:00:00"
        prior_records = [{"id": "page-before-interruption"}]

        class Checkpoint:
            def __init__(self):
                self.loaded = []

            def load(self, key):
                self.loaded.append(key)
                if key == unfinished_key:
                    return {
                        "records": prior_records,
                        "cursor": "saved-next-page-token",
                        "complete": False,
                    }
                return None

            def save(self, key, state):
                pass

        checkpoint = Checkpoint()
        resumed = []

        def fake_page_through(base_url, params, source, max_pages, started, max_seconds,
                              stats, initial_cursor=None, initial_results=None, on_page=None):
            if params["updatedFrom"] + "|" + params["updatedTo"] == unfinished_key:
                resumed.append((initial_cursor, list(initial_results or [])))
                stats["stopped"] = "time limit"
                return list(initial_results or []) + [{"id": "page-after-resume"}]
            return []

        with (
            patch.object(tenderscout, "datetime", Thursday),
            patch.object(tenderscout, "_page_through", side_effect=fake_page_through),
            patch.object(tenderscout.time, "sleep"),
        ):
            rows = tenderscout.fetch_fts(
                days=60, max_pages=10, max_seconds=150, checkpoint=checkpoint
            )

        self.assertIn(unfinished_key, checkpoint.loaded)
        self.assertEqual(
            resumed,
            [("saved-next-page-token", prior_records)],
        )
        self.assertEqual(
            rows,
            prior_records + [{"id": "page-after-resume"}],
        )


    def test_incomplete_six_hour_window_resumes_friday_with_saved_cursor_and_records(self):
        real_datetime = tenderscout.datetime

        class Friday(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

        unfinished_key = "2026-11-06T06:00:00|2026-11-06T12:00:00"
        prior_records = [{"id": "thursday-page-before-interruption"}]

        class Checkpoint:
            def __init__(self):
                self.loaded = []

            def load(self, key):
                self.loaded.append(key)
                if key == unfinished_key:
                    return {
                        "records": prior_records,
                        "cursor": "thursday-saved-next-page-token",
                        "complete": False,
                    }
                return None

            def save(self, key, state):
                pass

        checkpoint = Checkpoint()
        resumed = []

        def fake_page_through(base_url, params, source, max_pages, started, max_seconds,
                              stats, initial_cursor=None, initial_results=None, on_page=None):
            if params["updatedFrom"] + "|" + params["updatedTo"] == unfinished_key:
                resumed.append((initial_cursor, list(initial_results or [])))
                stats["stopped"] = "time limit"
                return list(initial_results or []) + [{"id": "friday-page-after-resume"}]
            return []

        with (
            patch.object(tenderscout, "datetime", Friday),
            patch.object(tenderscout, "_page_through", side_effect=fake_page_through),
            patch.object(tenderscout.time, "sleep"),
        ):
            rows = tenderscout.fetch_fts(
                days=60, max_pages=10, max_seconds=150, checkpoint=checkpoint
            )

        self.assertIn(unfinished_key, checkpoint.loaded)
        self.assertEqual(
            resumed,
            [("thursday-saved-next-page-token", prior_records)],
        )
        self.assertEqual(
            rows,
            prior_records + [{"id": "friday-page-after-resume"}],
        )


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
