import csv
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import tenderscout


class DeadlineParsingTests(unittest.TestCase):
    def test_date_only_deadline_remains_valid_through_end_of_date(self):
        deadline = tenderscout.parse_date("2026-10-25")
        self.assertEqual(deadline.isoformat(), "2026-10-25T23:59:59.999999+00:00")

    def test_explicit_deadline_time_is_preserved(self):
        deadline = tenderscout.parse_date("2026-10-25T12:30:00Z")
        self.assertEqual(deadline.isoformat(), "2026-10-25T12:30:00+00:00")


class OpportunityStageTests(unittest.TestCase):
    def test_extract_records_identifies_planning_stage(self):
        payload = {
            "releases": [{
                "id": "planning-1",
                "ocid": "ocds-test-1",
                "tag": ["planning"],
                "date": "2026-10-25T10:00:00Z",
                "tender": {"title": "Planned cleaning contract", "description": "Cleaning"},
                "buyer": {"name": "Test Council"},
            }]
        }
        rows = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["opportunity_stage"], "Planning / upcoming")

    def test_dedupe_prefers_open_tender_over_planning_for_same_process(self):
        rows = [
            {"ocid": "ocds-test-2", "title": "Cleaning", "buyer": "Council",
             "opportunity_stage": "Planning / upcoming"},
            {"ocid": "ocds-test-2", "title": "Cleaning", "buyer": "Council",
             "opportunity_stage": "Open for bids"},
        ]
        result = tenderscout.dedupe(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["opportunity_stage"], "Open for bids")


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

    def test_wider_area_postcodes_and_places_are_local(self):
        for text in ("Kirkby L32 8XX", "Huyton L36 1AA", "Prescot L34 1AA", "Warrington WA1 1AA",
                     "Lymm WA13 0AA", "Wigan WN1 1AA", "Leigh WN7 1AA", "Ormskirk L39 2AA",
                     "Skelmersdale WN8 6AA", "Chester CH1 1AA", "Ellesmere Port CH65 4AA",
                     "Neston CH64 3RD", "Northwich CW9 5AA", "Frodsham WA6 7AA", "Rainhill L35 5AB"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "LOCAL")

    def test_places_beyond_the_wider_area_are_out_of_area(self):
        for text in ("Manchester M1 1AA", "Preston PR1 2AA", "Bolton BL1 1AA", "Crewe CW1 2AA",
                     "Stockport SK1 1AA", "Blackpool FY1 1AA", "Chorley PR7 1AA",
                     "Cheshire East", "Chester-le-Street DH3 3AA", "Wrexham LL11 1AA"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "OUT_OF_AREA")

    def test_borderline_postcodes_go_to_review(self):
        for text in ("CH4 8AA", "Merseyside", "Cheshire", "Greater Manchester", "North West"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "REVIEW")

    def test_mixed_locations_go_to_review(self):
        self.assertEqual(self.classify("Liverpool L1 8JQ; Edinburgh EH1 1AA"), "REVIEW")

    def test_county_name_does_not_override_local_postcode(self):
        self.assertEqual(self.classify("Runcorn, Cheshire WA7 2PQ"), "LOCAL")

    def test_place_names_match_whole_words_only(self):
        # "Canterbury" contains "bury"; it must not be treated as Bury.
        self.assertEqual(self.classify("Canterbury"), "REVIEW")

    def test_full_postcode_in_notice_text_is_local(self):
        record = {"location_text": "", "title": "Cleaning at Civic Centre WA7 2PQ", "description": ""}
        geo_class, reason = tenderscout.classify_geography(record, "Target area")
        self.assertEqual(geo_class, "LOCAL")
        self.assertIn("notice text", reason)

    def test_notice_text_postcode_is_not_trusted_when_text_points_elsewhere(self):
        for description in (
            "Sites in Runcorn WA7 2PQ and Edinburgh EH1 1AA",
            "Sites at WA7 2PQ and across Scotland",
            "Sites at WA7 2PQ and Crewe CW1 2AA",
        ):
            with self.subTest(description=description):
                record = {"location_text": "", "title": "Cleaning", "description": description}
                self.assertEqual(tenderscout.classify_geography(record, "Target area")[0], "REVIEW")

    def test_north_west_is_still_accepted_as_an_alias(self):
        record = {"location_text": "Birkenhead CH41 6AB"}
        self.assertEqual(tenderscout.classify_geography(record, "North West")[0], "LOCAL")

    def test_uk_wide_skips_the_area_filter(self):
        record = {"location_text": "Edinburgh EH1 1AA"}
        self.assertEqual(tenderscout.classify_geography(record, "UK wide")[0], "UK-wide search")


class PostcodeCoverageTests(unittest.TestCase):
    def classify(self, location_text):
        return tenderscout.classify_geography({"location_text": location_text}, "Target area")[0]

    def test_relevant_postcode_areas_are_confirmed_local(self):
        # Representative postcode districts across the full agreed service area:
        # Liverpool, Sefton, Wirral, St Helens, Halton, Knowsley, Warrington,
        # Wigan, West Lancashire and Cheshire West/Chester.
        for postcode in (
            "L1 1AA", "L20 1AA", "PR8 1AA", "CH41 1AA", "CH63 1AA",
            "WA9 1AA", "WA7 1AA", "L32 1AA", "WA1 1AA", "WN1 1AA",
            "L39 1AA", "WN8 1AA", "CH1 1AA", "CH64 1AA", "CW9 1AA",
        ):
            with self.subTest(postcode=postcode):
                self.assertEqual(self.classify(postcode), "LOCAL")

    def test_postcodes_outside_the_service_area_are_not_local(self):
        for postcode in ("M1 1AA", "E1 1AA", "LS1 1AA"):
            with self.subTest(postcode=postcode):
                self.assertEqual(self.classify(postcode), "OUT_OF_AREA")

    def test_explicit_english_regions_outside_target_area_are_rejected(self):
        for region in (
            "East Midlands, United Kingdom",
            "South East, United Kingdom",
            "West Midlands, United Kingdom",
            "Yorkshire and the Humber, United Kingdom",
            "East of England, United Kingdom",
            "North East, United Kingdom",
            "South West, United Kingdom",
        ):
            with self.subTest(region=region):
                self.assertEqual(self.classify(region), "OUT_OF_AREA")

    def test_borderline_postcode_is_sent_for_review(self):
        self.assertEqual(self.classify("CH4 1AA"), "REVIEW")


class RegionCodeAndCommunityTests(unittest.TestCase):
    def classify(self, location_text):
        return tenderscout.classify_geography({"location_text": location_text}, "Target area")[0]

    def test_liverpool_sefton_wirral_region_codes_are_local(self):
        for code in ("UKD72", "UKD73", "UKD74", "ukd72"):
            with self.subTest(code=code):
                self.assertEqual(self.classify(code), "LOCAL")

    def test_wider_area_region_codes_are_local(self):
        for code in ("UKD61", "UKD63", "UKD71"):
            with self.subTest(code=code):
                self.assertEqual(self.classify(code), "LOCAL")

    def test_codes_covering_the_area_and_more_need_checking(self):
        for code in ("UKD", "UKD3", "UKD36", "UKD47", "UKD6", "UKD7"):
            with self.subTest(code=code):
                self.assertEqual(self.classify(code), "REVIEW")

    def test_a_broad_code_is_confirmed_by_a_place_or_postcode(self):
        self.assertEqual(self.classify("UKD36 Wigan"), "LOCAL")
        self.assertEqual(self.classify("UKD47 Ormskirk"), "LOCAL")
        self.assertEqual(self.classify("UKD36 Bolton"), "OUT_OF_AREA")
        self.assertEqual(self.classify("UKD36 Leigh"), "REVIEW")
        self.assertEqual(self.classify("Merseyside, Edinburgh"), "OUT_OF_AREA")
        self.assertEqual(self.classify("Greater Manchester, Bolton"), "OUT_OF_AREA")

    def test_region_codes_for_other_areas_are_out_of_area(self):
        for code in ("UKM50", "UKM", "UKL12", "UKN06", "UKI31", "UKD33", "UKD62", "UKD41", "UKC11"):
            with self.subTest(code=code):
                self.assertEqual(self.classify(code), "OUT_OF_AREA")

    def test_region_codes_do_not_match_ordinary_words(self):
        for text in ("UKAS accredited", "UKHSA site", "UKCA marked", "UKRI funded"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "REVIEW")

    def test_local_neighbourhoods_are_recognised(self):
        for place in ("Wavertree", "Aigburth", "Ainsdale", "Hoylake", "West Kirby", "Bebington",
                      "Rainford", "Newton-le-Willows", "Daresbury", "Widnes", "Port Sunlight"):
            with self.subTest(place=place):
                self.assertEqual(self.classify(place), "LOCAL")

    def test_names_shared_with_other_places_need_checking(self):
        for place in ("Woolton Hill", "Garston", "Halton", "Walton", "Sutton", "Leigh", "Kirkby", "Whiston", "Orrell"):
            with self.subTest(place=place):
                self.assertEqual(self.classify(place), "REVIEW")

    def test_ambiguous_name_is_confirmed_by_a_postcode(self):
        self.assertEqual(self.classify("Woolton, Liverpool L25 7TA"), "LOCAL")
        self.assertEqual(self.classify("Halton Borough Council, Runcorn WA7 2PQ"), "LOCAL")

    def test_liverpool_street_london_is_not_liverpool(self):
        self.assertEqual(self.classify("Liverpool Street, London"), "OUT_OF_AREA")


class OverlapAndTradeTests(unittest.TestCase):
    def classify(self, location_text):
        return tenderscout.classify_geography({"location_text": location_text}, "Target area")[0]

    def test_longer_names_win_over_the_shorter_name_inside_them(self):
        self.assertEqual(self.classify("Preston Brook, Runcorn"), "LOCAL")
        self.assertEqual(self.classify("Cheshire West and Chester"), "LOCAL")
        self.assertEqual(self.classify("Cheshire East"), "OUT_OF_AREA")
        self.assertEqual(self.classify("Preston"), "OUT_OF_AREA")

    def score(self, title, description="", location_text="Liverpool L2 2DH", services=None):
        record = {
            "title": title, "description": description, "buyer": "Test buyer",
            "location_text": location_text, "estimated_value_gbp": "",
            "deadline": "2099-12-31T00:00:00+00:00",
        }
        return tenderscout.score_record(
            record, services or tenderscout.SERVICE_ORDER, "Target area", None, None)

    def test_each_trade_is_recognised_and_labelled(self):
        cases = {
            "Commercial cleaning of council offices": "Cleaning",
            "Soft FM and caretaking services": "Facilities management",
            "Responsive repairs and void works for housing stock": "Property maintenance",
            "Painting and decorating framework": "Property maintenance",
            "Roof repair programme": "Property maintenance",
            "School refurbishment and extension works": "Construction",
            "Demolition and groundworks": "Construction",
            "New build housing development": "Construction",
        }
        for title, label in cases.items():
            with self.subTest(title=title):
                result = self.score(title)
                self.assertIsNotNone(result)
                self.assertEqual(result["service_match"], label)

    def test_unrelated_tenders_are_ignored(self):
        for title in ("Supply of laptops", "Software licences",
                      "Legal advice framework"):
            with self.subTest(title=title):
                self.assertIsNone(self.score(title))

    def test_all_trades_are_in_scope_by_default(self):
        for title, expected in (
            ("Catering services", "Catering and food services"),
            ("Roofing and roof repairs", "Roofing and roof repairs"),
            ("IT and telecoms support", "IT and telecoms"),
            ("Arboricultural services - tree planting and maintenance", "Tree surgery"),
            ("RA360690 - Boiler Inspection", "Heating and boilers"),
            ("Provision of Mechanical and Electrical Services", "Electrical installation and repairs"),
            ("PA Unadopted Highways hard surface repairs", "Roads and paving"),
            ("Provision of Project Management & Cost Consultant Services", "Construction project management"),
        ):
            with self.subTest(title=title):
                result = self.score(title)
                self.assertIsNotNone(result)
                self.assertEqual(result["trade_category"], expected)

    def test_a_trade_can_be_switched_off(self):
        self.assertIsNone(self.score("Building refurbishment", services=["cleaning", "fm"]))
        self.assertIsNotNone(self.score("Building refurbishment", services=["construction"]))

    def test_cleaning_wins_when_a_tender_matches_several_trades(self):
        result = self.score("Cleaning after refurbishment works")
        self.assertEqual(result["service_match"], "Cleaning")


class DeliveryLocationExtractionRegressionTests(unittest.TestCase):
    def test_extracts_nested_delivery_site_from_tender_items(self):
        payload = {
            "releases": [{
                "id": "2026-1234",
                "buyer": {
                    "name": "Example Council",
                    "address": {"streetAddress": "Buyer HQ", "postalCode": "EH1 1AA", "locality": "Edinburgh"},
                },
                "tender": {
                    "title": "Commercial cleaning",
                    "items": [{
                        "deliveryLocation": {
                            "address": {
                                "streetAddress": "Civic Centre",
                                "postalCode": "L2 2DH",
                                "locality": "Liverpool",
                            }
                        }
                    }],
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertIn("L2 2DH", records[0]["location_text"])
        self.assertIn("Liverpool", records[0]["location_text"])
        self.assertEqual(
            tenderscout.classify_geography(records[0], "Target area")[0], "LOCAL"
        )

    def test_buyer_headquarters_alone_never_confirms_local_delivery(self):
        payload = {
            "releases": [{
                "id": "2026-1235",
                "buyer": {
                    "name": "Liverpool Council",
                    "address": {"streetAddress": "Town Hall", "postalCode": "L2 2DH", "locality": "Liverpool"},
                },
                "tender": {
                    "title": "Cleaning contract",
                    "description": "Routine cleaning at facilities",
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["location_text"], "")
        self.assertEqual(
            tenderscout.classify_geography(records[0], "Target area")[0], "REVIEW"
        )

    def test_explicit_nonlocal_delivery_site_overrides_local_buyer_headquarters(self):
        payload = {
            "releases": [{
                "id": "2026-1236",
                "buyer": {
                    "name": "Liverpool Council",
                    "address": {"postalCode": "L2 2DH", "locality": "Liverpool"},
                },
                "tender": {
                    "title": "Commercial cleaning",
                    "items": [{
                        "deliveryLocation": {
                            "address": {"postalCode": "EH1 1AA", "locality": "Edinburgh"}
                        }
                    }],
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertIn("EH1 1AA", records[0]["location_text"])
        self.assertNotIn("L2 2DH", records[0]["location_text"])
        self.assertEqual(
            tenderscout.classify_geography(records[0], "Target area")[0], "OUT_OF_AREA"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FindATenderRequestTests(unittest.TestCase):
    def test_fetch_does_not_use_incomplete_stage_filter(self):
        captured = []

        def fake_page(base_url, params, source, max_pages, started, max_seconds, stats):
            captured.append((base_url, dict(params), source))
            return []

        with patch.object(tenderscout, "_page_through", side_effect=fake_page):
            tenderscout.fetch_fts(days=1, max_pages=1, max_seconds=1, stats={})

        self.assertTrue(captured)
        self.assertTrue(all(source == "Find a Tender" for _, _, source in captured))
        self.assertTrue(all("stages" not in params for _, params, _ in captured))
        self.assertTrue(all(params["limit"] == "100" for _, params, _ in captured))
