import csv
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import tenderscout


class GeographyClassifierTests(unittest.TestCase):
    def test_confirmed_liverpool_location_is_local(self):
        record = {"location_text": "Liverpool"}
        geo_class, reason = tenderscout.classify_geography(record, "North West")
        self.assertEqual(geo_class, "LOCAL")
        self.assertIn("liverpool", reason.lower())

    def test_confirmed_southport_or_formby_location_is_local(self):
        for place in ("Southport", "Formby"):
            with self.subTest(place=place):
                record = {"location_text": place}
                geo_class, _ = tenderscout.classify_geography(record, "North West")
                self.assertEqual(geo_class, "LOCAL")

    def test_confirmed_st_helens_location_is_local(self):
        record = {"location_text": "St Helens"}
        geo_class, reason = tenderscout.classify_geography(record, "North West")
        self.assertEqual(geo_class, "LOCAL")
        self.assertIn("st helens", reason.lower())

    def test_confirmed_leeds_or_falkirk_location_is_out_of_area(self):
        for place in ("Leeds", "Falkirk"):
            with self.subTest(place=place):
                record = {"location_text": place}
                geo_class, reason = tenderscout.classify_geography(record, "North West")
                self.assertEqual(geo_class, "OUT_OF_AREA")
                self.assertIn("outside", reason.lower())

    def test_missing_location_is_review(self):
        record = {"location_text": "", "title": "Cleaning contract", "description": "Routine cleaning at facilities"}
        geo_class, reason = tenderscout.classify_geography(record, "North West")
        self.assertEqual(geo_class, "REVIEW")
        self.assertIn("location", reason.lower())

    def test_local_place_in_title_only_is_review(self):
        record = {
            "location_text": "",
            "title": "Cleaning services in Liverpool",
            "description": "Routine janitorial services for local council premises.",
        }
        geo_class, reason = tenderscout.classify_geography(record, "North West")
        self.assertEqual(geo_class, "REVIEW")
        self.assertIn("title/description", reason.lower())

    def test_review_tenders_appear_in_review_csv_and_not_main_shortlist(self):
        review_record = {
            "source": "Find a Tender",
            "published_date": "2026-10-01",
            "title": "Cleaning services in Liverpool",
            "buyer": "Liverpool Council",
            "description": "Routine cleaning and janitorial support.",
            "location_text": "",
            "estimated_value_gbp": 150000,
            "deadline": "2099-12-31T00:00:00+00:00",
            "status": "tender",
            "notice_url": "https://example.com/review-notice",
            "ocid": "review-001",
        }
        local_record = {
            "source": "Find a Tender",
            "published_date": "2026-10-02",
            "title": "Cleaning contract",
            "buyer": "Sefton Council",
            "description": "Routine cleaning services.",
            "location_text": "Southport",
            "estimated_value_gbp": 120000,
            "deadline": "2099-12-31T00:00:00+00:00",
            "status": "tender",
            "notice_url": "https://example.com/local-notice",
            "ocid": "local-001",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = os.getcwd()
            os.chdir(temp_dir)
            try:
                with patch.object(tenderscout, "fetch_fts", return_value=[review_record, local_record]), \
                     patch.object(tenderscout, "fetch_contracts_finder", return_value=[]), \
                     patch.object(sys, "argv", [
                         "tenderscout.py",
                         "--days", "14",
                         "--region", "North West",
                         "--services", "cleaning",
                         "--min-score", "3",
                         "--pages", "1",
                         "--out", "shortlist.csv",
                     ]):
                    tenderscout.main()

                with open(os.path.join(temp_dir, "shortlist.csv"), newline="", encoding="utf-8-sig") as csv_file:
                    shortlist_rows = list(csv.DictReader(csv_file))
                self.assertEqual(len(shortlist_rows), 1)
                self.assertEqual(shortlist_rows[0]["ocid"], "local-001")
                self.assertNotIn("review-001", [row.get("ocid", "") for row in shortlist_rows])

                with open(os.path.join(temp_dir, "tenderscout_review.csv"), newline="", encoding="utf-8-sig") as csv_file:
                    review_rows = list(csv.DictReader(csv_file))
                self.assertEqual(len(review_rows), 1)
                self.assertEqual(review_rows[0]["ocid"], "review-001")
                self.assertIn("geography_reason", review_rows[0])
                self.assertIn("title/description", review_rows[0]["geography_reason"].lower())
            finally:
                os.chdir(original_cwd)


class PostcodeAreaTests(unittest.TestCase):
    def classify(self, location_text, **extra):
        record = {"location_text": location_text, **extra}
        return tenderscout.classify_geography(record, "Target area")[0]

    def test_target_area_postcodes_are_local(self):
        cases = {
            "Liverpool": "Cleaning Unit, Dale St, L2 2DH",
            "Liverpool L18": "Allerton Rd, L18 1AB",
            "Sefton": "Lord St, Southport PR8 1AA",
            "Sefton Bootle": "Stanley Rd, L20 3AA",
            "Wirral": "Hamilton Sq, Birkenhead CH41 6AB",
            "Wirral Bebington": "Port Sunlight CH62 5AA",
            "St Helens": "Corporation St, WA10 1AA",
            "Halton Runcorn": "Runcorn WA7 2PQ",
            "Halton Widnes": "Widnes WA8 7AA",
            "No space": "L18JQ",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.classify(text), "LOCAL")

    def test_bare_district_codes_are_recognised(self):
        for text in ("WA9", "CH43", "PR9 area"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "LOCAL")

    def test_scotland_is_out_of_area(self):
        for text in (
            "Edinburgh EH1 1AA",
            "Glasgow G2 4QE",
            "Aberdeen AB10 1AA",
            "Perth PH1 5QA",
            "Scotland",
            "Fife",
            "South Lanarkshire",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "OUT_OF_AREA")

    def test_knowsley_and_neighbours_are_out_of_area(self):
        for text in ("Kirkby L32 8XX", "Huyton L36 1AA", "Warrington WA1 1AA",
                     "Ormskirk L39 2AA", "Ellesmere Port CH65 4AA", "Manchester M1 1AA"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "OUT_OF_AREA")

    def test_borderline_postcodes_go_to_review(self):
        for text in ("L35 5AB", "Neston CH64 3RD", "Merseyside", "Cheshire"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "REVIEW")

    def test_mixed_locations_go_to_review(self):
        self.assertEqual(self.classify("Liverpool L1 8JQ; Edinburgh EH1 1AA"), "REVIEW")

    def test_county_name_does_not_override_local_postcode(self):
        self.assertEqual(self.classify("Runcorn, Cheshire WA7 2PQ"), "LOCAL")

    def test_place_names_match_whole_words_only(self):
        # "Canterbury" contains "bury"; it must not be treated as Bury.
        self.assertEqual(self.classify("Canterbury"), "REVIEW")

    def test_postcode_in_title_only_is_review(self):
        record = {"location_text": "", "title": "Cleaning at Civic Centre WA7 2PQ", "description": ""}
        self.assertEqual(tenderscout.classify_geography(record, "Target area")[0], "REVIEW")

    def test_north_west_is_still_accepted_as_an_alias(self):
        record = {"location_text": "Birkenhead CH41 6AB"}
        self.assertEqual(tenderscout.classify_geography(record, "North West")[0], "LOCAL")

    def test_uk_wide_skips_the_area_filter(self):
        record = {"location_text": "Edinburgh EH1 1AA"}
        self.assertEqual(tenderscout.classify_geography(record, "UK wide")[0], "UK-wide search")


if __name__ == "__main__":
    unittest.main(verbosity=2)
