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

    def test_genuine_knowsley_delivery_address_is_local(self):
        payload = {
            "releases": [{
                "ocid": "ocds-knowsley-001",
                "buyer": {"name": "Knowsley Council"},
                "tender": {
                    "title": "Window cleaning services",
                    "items": [{
                        "description": "External glass cleaning",
                        "deliveryAddress": {
                            "streetAddress": "1 Huyton Road",
                            "locality": "Knowsley",
                            "postalCode": "L36 0JX",
                            "countryName": "United Kingdom",
                        },
                    }],
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertIn("Knowsley", records[0]["location_text"])
        self.assertEqual(tenderscout.classify_geography(records[0], "North West")[0], "LOCAL")

    def test_manchester_buyer_without_delivery_address_is_review(self):
        payload = {
            "releases": [{
                "ocid": "ocds-manchester-001",
                "buyer": {
                    "name": "Manchester City Council",
                    "address": {"locality": "Manchester", "region": "Greater Manchester"},
                },
                "tender": {
                    "title": "Cleaning services",
                    "description": "Routine cleaning and janitorial support",
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["location_text"], "")
        self.assertEqual(tenderscout.classify_geography(records[0], "North West")[0], "REVIEW")

    def test_leeds_delivery_address_is_out_of_area(self):
        payload = {
            "releases": [{
                "ocid": "ocds-leeds-001",
                "buyer": {"name": "Leeds City Council"},
                "tender": {
                    "title": "Cleaning services",
                    "items": [{
                        "deliveryAddress": {
                            "locality": "Leeds",
                            "region": "West Yorkshire",
                        }
                    }],
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertIn("Leeds", records[0]["location_text"])
        self.assertEqual(tenderscout.classify_geography(records[0], "North West")[0], "OUT_OF_AREA")

    def test_title_only_knowsley_reference_is_review(self):
        record = {
            "location_text": "",
            "title": "Cleaning contract in Knowsley",
            "description": "Routine cleaning services for local council operations.",
        }
        geo_class, reason = tenderscout.classify_geography(record, "North West")
        self.assertEqual(geo_class, "REVIEW")
        self.assertIn("title/description", reason.lower())

    def test_target_area_postcodes_are_local(self):
        for postcode in ("L3 8AA", "L24 9AB", "L20 3XX", "WA7 1AA", "WA9 3AA", "CH25 9AB"):
            with self.subTest(postcode=postcode):
                geo_class, reason = tenderscout.classify_geography({"location_text": postcode}, "North West")
                self.assertEqual(geo_class, "LOCAL", postcode)
                self.assertIn("postcode", reason.lower())

    def test_outside_postcode_districts_are_out_of_area(self):
        for postcode in ("LS1 1AA", "M1 1AE", "B1 1AA", "NE1 1AA"):
            with self.subTest(postcode=postcode):
                geo_class, reason = tenderscout.classify_geography({"location_text": postcode}, "North West")
                self.assertEqual(geo_class, "OUT_OF_AREA", postcode)
                self.assertIn("outside", reason.lower())

    def test_malformed_postcodes_are_review(self):
        for postcode in ("ABC 123", "L3", "L3 4", "WA 123"):
            with self.subTest(postcode=postcode):
                geo_class, reason = tenderscout.classify_geography({"location_text": postcode}, "North West")
                self.assertEqual(geo_class, "REVIEW", postcode)
                self.assertIn("review", reason.lower())

    def test_conflicting_local_and_out_of_area_postcodes_are_review(self):
        record = {"location_text": "L24 9AB, LS1 1AA"}
        geo_class, reason = tenderscout.classify_geography(record, "North West")
        self.assertEqual(geo_class, "REVIEW")
        self.assertIn("multiple", reason.lower())

    def test_multi_site_contract_with_local_and_non_local_delivery_sites_is_review(self):
        record = {"location_text": "WA9 3AA and LS1 1AA"}
        geo_class, reason = tenderscout.classify_geography(record, "North West")
        self.assertEqual(geo_class, "REVIEW")
        self.assertIn("multiple", reason.lower())

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
















































































































































































