import unittest
from unittest.mock import patch

import tenderscout


class FindATenderPaginationTests(unittest.TestCase):
    def test_hosted_collector_pages_deeper_into_two_newest_windows(self):
        windows = [
            ("2026-10-21T00:00:00", "2026-10-28T00:00:00"),
            ("2026-10-14T00:00:00", "2026-10-21T00:00:00"),
            ("2026-10-07T00:00:00", "2026-10-14T00:00:00"),
        ]
        calls = []

        def fake_page_through(base_url, params, source, max_pages, started, max_seconds, stats):
            calls.append(max_pages)
            return []

        with patch.object(tenderscout, "_windows", return_value=windows), \
             patch.object(tenderscout, "_page_through", side_effect=fake_page_through), \
             patch.object(tenderscout.time, "sleep"):
            result = tenderscout.fetch_fts(days=21, max_pages=10, max_seconds=150)

        self.assertEqual(result, [])
        self.assertEqual(calls, [4, 4, 1])

    def test_cli_audit_page_limit_is_respected_without_hosted_time_budget(self):
        windows = [
            ("2026-10-21T00:00:00", "2026-10-28T00:00:00"),
            ("2026-10-14T00:00:00", "2026-10-21T00:00:00"),
        ]
        calls = []

        def fake_page_through(base_url, params, source, max_pages, started, max_seconds, stats):
            calls.append(max_pages)
            return []

        with patch.object(tenderscout, "_windows", return_value=windows), \
             patch.object(tenderscout, "_page_through", side_effect=fake_page_through), \
             patch.object(tenderscout.time, "sleep"):
            tenderscout.fetch_fts(days=14, max_pages=10)

        self.assertEqual(calls, [10, 10])


if __name__ == "__main__":
    unittest.main()
