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


class FindATenderWindowCoverageTests(unittest.TestCase):
    def test_searches_each_date_window_before_paging_deeply_into_latest_week(self):
        windows = [
            (tenderscout.datetime(2026, 10, 1, tzinfo=tenderscout.timezone.utc),
             tenderscout.datetime(2026, 10, 8, tzinfo=tenderscout.timezone.utc)),
            (tenderscout.datetime(2026, 10, 8, tzinfo=tenderscout.timezone.utc),
             tenderscout.datetime(2026, 10, 15, tzinfo=tenderscout.timezone.utc)),
            (tenderscout.datetime(2026, 10, 15, tzinfo=tenderscout.timezone.utc),
             tenderscout.datetime(2026, 10, 22, tzinfo=tenderscout.timezone.utc)),
        ]
        def page(index):
            return {
                "releases": [{
                    "id": f"release-{index}",
                    "date": "2026-10-01T12:00:00Z",
                    "tender": {"title": f"Cleaning contract {index}"},
                    "buyer": {"name": "Example Council"},
                }],
                "nextCursor": f"cursor-{index}",
            }
        with patch("tenderscout._windows", return_value=windows), \
             patch("tenderscout.get_json", side_effect=[page(1), page(2), page(3)]) as fetch, \
             patch("tenderscout.time.sleep"):
            stats = {}
            rows = tenderscout.fetch_fts(days=21, max_pages=25, max_seconds=150, stats=stats)

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(stats["windows"], 3)
        self.assertEqual(stats["pages"], 3)
        self.assertTrue(stats["more"])


class StandstillDiscoveryFeedTests(unittest.TestCase):
    def test_public_rss_is_parsed_as_discovery_not_confirmed_delivery_location(self):
        payload = b'''<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>Roof repairs in Liverpool</title>
          <link>https://standstill.uk/tenders/roof-repairs</link>
          <guid>standstill-roof-1</guid>
          <pubDate>Tue, 27 Oct 2026 10:00:00 GMT</pubDate>
          <description><![CDATA[Roof maintenance services. Official notice:
            <a href="https://www.find-tender.service.gov.uk/Notice/123456-2026">notice</a>]]></description>
        </item></channel></rss>'''
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return payload

        with patch("tenderscout.urllib.request.urlopen", return_value=FakeResponse()) as mock_urlopen:
            stats = {}
            rows = tenderscout.fetch_standstill_northwest(stats=stats)

        self.assertEqual(mock_urlopen.call_count, 7)
        self.assertEqual(stats["windows"], 7)
        self.assertIn("Cleaning CPV 90910000", stats["feed_counts"])
        self.assertIn("Construction CPV 45000000", stats["feed_counts"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats["records"], 1)
        self.assertEqual(rows[0]["source"], "Standstill UK (RSS discovery; verify official notice)")
        self.assertIn("North West England", rows[0]["location_text"])
        self.assertEqual(rows[0]["notice_url"], "https://www.find-tender.service.gov.uk/Notice/123456-2026")
        self.assertEqual(
            tenderscout.classify_geography(rows[0], "Target area")[0],
            "REVIEW",
        )

    def test_dedupe_merges_cross_source_copies_by_official_notice_url(self):
        official = {
            "ocid": "ocds-official-1", "notice_url": "https://www.find-tender.service.gov.uk/Notice/123456-2026",
            "title": "Roof repairs in Liverpool", "buyer": "Council", "published_date": "2026-10-27",
            "opportunity_stage": "Open for bids",
        }
        discovery = {
            "ocid": "standstill-roof-1", "notice_url": "https://www.find-tender.service.gov.uk/Notice/123456-2026",
            "title": "Roof repairs in Liverpool", "buyer": "Council", "published_date": "2026-10-27",
            "opportunity_stage": "Open for bids",
        }
        rows = tenderscout.dedupe([official, discovery])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ocid"], "ocds-official-1")


class TradeContextExtractionTests(unittest.TestCase):
    def test_item_classification_and_description_are_preserved_for_trade_matching(self):
        payload = {
            "releases": [{
                "id": "item-trade-1",
                "ocid": "ocds-test-item-trade-1",
                "date": "2026-10-25T10:00:00Z",
                "tag": ["tender"],
                "tender": {
                    "title": "Property services framework",
                    "description": "Framework for planned works.",
                    "items": [{
                        "description": "Commercial and office cleaning",
                        "classification": {"id": "90910000", "description": "Cleaning services"},
                    }],
                },
                "buyer": {"name": "Test Council"},
            }]
        }
        rows = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(rows), 1)
        self.assertIn("Commercial and office cleaning", rows[0]["trade_context"])
        self.assertIn("Cleaning services", rows[0]["trade_context"])
        scored = tenderscout.score_record(rows[0], tenderscout.SERVICE_ORDER, "North West", None, None)
        self.assertIsNotNone(scored)
        self.assertEqual(scored["trade_category"], "Commercial and office cleaning")


    def test_building_cleaning_is_classified_as_commercial_cleaning(self):
        record = {
            "title": "CA18428 - West Derby School - Provision of Building Cleaning Services",
            "description": "",
            "buyer": "West Derby School",
            "trade_context": "",
            "location_text": "L13 7HQ",
            "deadline": "2099-10-16T10:00:00+01:00",
            "opportunity_stage": "Open for bids",
        }
        scored = tenderscout.score_record(
            record, tenderscout.SERVICE_ORDER, "Target area", None, None
        )
        self.assertIsNotNone(scored)
        self.assertEqual(scored["trade_category"], "Commercial and office cleaning")


class OpportunityStageTests(unittest.TestCase):
    def test_only_open_or_planned_notices_are_shortlist_eligible(self):
        self.assertTrue(tenderscout.shortlist_eligible_stage({"opportunity_stage": "Open for bids"}))
        self.assertTrue(tenderscout.shortlist_eligible_stage({"opportunity_stage": "Planning / upcoming"}))
        self.assertFalse(tenderscout.shortlist_eligible_stage({"opportunity_stage": "Other notice"}))

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
                     patch.object(tenderscout, "fetch_bearings", return_value=[]), \
                     patch.object(tenderscout, "fetch_standstill_northwest", return_value=[]), \
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
            "St Helens": "Corporation St, WA10 1AA",
            "Halton Runcorn": "Runcorn WA7 2PQ",
            "Halton Widnes": "Widnes WA8 7AA",
            "No space": "L18JQ",
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.classify(text), "LOCAL")

    def test_bare_district_codes_are_recognised(self):
        for text in ("WA9", "PR9 area"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "LOCAL")

    def test_wirral_towns_and_postcodes_are_included_in_service_area(self):
        for text in (
            "Wirral", "Birkenhead CH41 6AB", "Bebington CH62 5AA",
            "Wallasey CH45 4AA", "Wallasey CH44", "CH43",
            "Moreton CH46 6AA", "West Kirby CH48 4AA",
        ):
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

    def test_knowsley_st_helens_and_halton_remain_local(self):
        for text in ("Kirkby L32 8XX", "Huyton L36 1AA", "Prescot L34 1AA",
                     "Rainhill WA9 3AB", "Runcorn WA7 2PQ", "Widnes WA8 7AA"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "LOCAL")

    def test_wider_north_west_places_are_outside_initial_scope(self):
        for text in ("Warrington WA1 1AA", "Lymm WA13 0AA", "Wigan WN1 1AA",
                     "Leigh WN7 1AA", "Ormskirk L39 2AA", "Skelmersdale WN8 6AA",
                     "Chester CH1 1AA", "Ellesmere Port CH65 4AA",
                     "Neston CH64 3RD", "Northwich CW9 5AA", "Frodsham WA6 7AA"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "OUT_OF_AREA")

    def test_places_beyond_the_wider_area_are_out_of_area(self):
        for text in ("Manchester M1 1AA", "Preston PR1 2AA", "Bolton BL1 1AA", "Crewe CW1 2AA",
                     "Stockport SK1 1AA", "Blackpool FY1 1AA", "Chorley PR7 1AA",
                     "Cheshire East", "Chester-le-Street DH3 3AA", "Wrexham LL11 1AA"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "OUT_OF_AREA")

    def test_borderline_postcodes_go_to_review(self):
        for text in ("Merseyside", "Cheshire", "Greater Manchester", "North West"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "REVIEW")

    def test_mixed_locations_go_to_review(self):
        self.assertEqual(self.classify("Liverpool L1 8JQ; Edinburgh EH1 1AA"), "REVIEW")

    def test_county_name_does_not_override_local_postcode(self):
        self.assertEqual(self.classify("Runcorn, Cheshire WA7 2PQ"), "LOCAL")

    def test_place_names_match_whole_words_only(self):
        # "Canterbury" contains "bury"; it must not be treated as Bury.
        self.assertEqual(self.classify("Canterbury"), "REVIEW")

    def test_full_postcode_in_title_or_description_requires_review(self):
        for field, value in (
            ("title", "Cleaning at Civic Centre WA7 2PQ"),
            ("description", "The service may cover sites at WA7 2PQ"),
            ("description", "Cleaning services in Leeds LS1 1AA"),
        ):
            with self.subTest(field=field, value=value):
                record = {"location_text": "", "title": "Cleaning", "description": ""}
                record[field] = value
                geo_class, reason = tenderscout.classify_geography(record, "Target area")
                self.assertEqual(geo_class, "REVIEW")
                self.assertIn("title/description", reason)

    def test_notice_text_postcode_is_not_trusted_when_text_points_elsewhere(self):
        for description in (
            "Sites in Runcorn WA7 2PQ and Edinburgh EH1 1AA",
            "Sites at WA7 2PQ and across Scotland",
            "Sites at WA7 2PQ and Crewe CW1 2AA",
        ):
            with self.subTest(description=description):
                record = {"location_text": "", "title": "Cleaning", "description": description}
                self.assertEqual(tenderscout.classify_geography(record, "Target area")[0], "REVIEW")

    def test_mixed_area_contract_is_review_not_discarded_as_out_of_area(self):
        record = {
            "title": "Cleaning services across schools",
            "description": (
                "Schools located across Manchester, Trafford, Knowsley and Wigan; "
                "routine and periodic cleaning across 11 schools."
            ),
            "location_text": (
                "M33 5BP, British Oversea Territories, Channel Islands, Europe, "
                "Isle of Man, Rest of the World, United Kingdom"
            ),
        }
        geo_class, reason = tenderscout.classify_geography(record, "Target area")
        self.assertEqual(geo_class, "REVIEW")
        self.assertIn("knowsley", reason.lower())
        self.assertNotEqual(geo_class, "LOCAL")

    def test_wirral_is_included_in_target_area(self):
        for location in ("Birkenhead CH41 6AB", "Wirral", "Wallasey CH45 4AA"):
            with self.subTest(location=location):
                self.assertEqual(
                    tenderscout.classify_geography({"location_text": location}, "Target area")[0],
                    "LOCAL",
                )

    def test_uk_wide_skips_the_area_filter(self):
        record = {"location_text": "Edinburgh EH1 1AA"}
        self.assertEqual(tenderscout.classify_geography(record, "UK wide")[0], "UK-wide search")


class PostcodeCoverageTests(unittest.TestCase):
    def classify(self, location_text):
        return tenderscout.classify_geography({"location_text": location_text}, "Target area")[0]

    def test_relevant_postcode_areas_are_confirmed_local(self):
        # Only the agreed five initial service areas are confirmed local.
        for postcode in (
            "L1 1AA", "L20 1AA", "PR8 1AA", "WA9 1AA", "WA7 1AA", "L32 1AA",
        ):
            with self.subTest(postcode=postcode):
                self.assertEqual(self.classify(postcode), "LOCAL")

    def test_postcodes_outside_the_service_area_are_not_local(self):
        for postcode in ("M1 1AA", "E1 1AA", "LS1 1AA", "WA1 1AA", "WN1 1AA", "L39 1AA", "CH1 1AA", "CW9 1AA"):
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

    def test_liverpool_and_sefton_region_codes_are_local(self):
        for code in ("UKD71", "UKD72", "UKD73", "UKD74", "ukd72"):
            with self.subTest(code=code):
                self.assertEqual(self.classify(code), "LOCAL")

    def test_only_agreed_merseyside_region_codes_are_local(self):
        for code in ("UKD61", "UKD63"):
            with self.subTest(code=code):
                self.assertEqual(self.classify(code), "OUT_OF_AREA")
        self.assertEqual(self.classify("UKD71"), "LOCAL")

    def test_codes_covering_the_area_and_more_need_checking(self):
        for code in ("UKD", "UKD3", "UKD36", "UKD47", "UKD6", "UKD7"):
            with self.subTest(code=code):
                self.assertEqual(self.classify(code), "REVIEW")

    def test_a_broad_code_is_confirmed_by_a_place_or_postcode(self):
        self.assertEqual(self.classify("UKD36 Wigan"), "OUT_OF_AREA")
        self.assertEqual(self.classify("UKD47 Ormskirk"), "OUT_OF_AREA")
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
        for place in ("Wavertree", "Aigburth", "Ainsdale",
                      "Rainford", "Newton-le-Willows", "Daresbury", "Widnes"):
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
        self.assertEqual(self.classify("Cheshire West and Chester"), "OUT_OF_AREA")
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


class ContractMetadataExtractionTests(unittest.TestCase):
    def test_value_and_contract_period_are_extracted_for_plain_english_summary(self):
        payload = {
            "releases": [{
                "id": "2026-9870",
                "date": "2026-10-01T00:00:00Z",
                "tag": ["planning"],
                "buyer": {"name": "Example Council"},
                "tender": {
                    "title": "Cleaning services",
                    "value": {"amount": 125000, "currency": "GBP"},
                    "contractPeriod": {
                        "startDate": "2027-04-01T00:00:00Z",
                        "endDate": "2029-03-31T23:59:59Z",
                    },
                    "tenderPeriod": {"endDate": "2027-01-15T12:00:00Z"},
                    "items": [{
                        "deliveryLocation": {
                            "address": {"locality": "Liverpool", "postalCode": "L2 2DH"}
                        }
                    }],
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["estimated_value_gbp"], 125000.0)
        self.assertEqual(records[0]["contract_start_date"], "2027-04-01T00:00:00Z")
        self.assertEqual(records[0]["contract_end_date"], "2029-03-31T23:59:59Z")
        self.assertEqual(records[0]["opportunity_stage"], "Planning / upcoming")


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


class DeliveryLocationDescriptionTests(unittest.TestCase):
    def test_extracts_description_inside_explicit_delivery_location(self):
        payload = {
            "releases": [{
                "id": "2026-9876",
                "buyer": {
                    "name": "Example Council",
                    "address": {"postalCode": "EH1 1AA", "locality": "Edinburgh"},
                },
                "tender": {
                    "title": "Cleaning services",
                    "description": "General tender description must not be treated as a site",
                    "items": [{
                        "deliveryLocation": {
                            "description": "Sites across Liverpool and Knowsley"
                        }
                    }],
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertIn("Sites across Liverpool and Knowsley", records[0]["location_text"])
        self.assertEqual(
            tenderscout.classify_geography(records[0], "Target area")[0], "LOCAL"
        )
        self.assertNotIn("General tender description", records[0]["location_text"])

    def test_does_not_treat_general_tender_description_as_delivery_location(self):
        payload = {
            "releases": [{
                "id": "2026-9877",
                "buyer": {
                    "name": "Liverpool Council",
                    "address": {"postalCode": "L2 2DH", "locality": "Liverpool"},
                },
                "tender": {
                    "title": "Cleaning services",
                    "description": "Services will be delivered in Liverpool",
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["location_text"], "")
        self.assertEqual(
            tenderscout.classify_geography(records[0], "Target area")[0], "REVIEW"
        )



class AdditionalDeliveryLocationSchemaTests(unittest.TestCase):
    def test_place_of_performance_description_is_used_as_site_evidence(self):
        payload = {
            "releases": [{
                "id": "2026-9878",
                "buyer": {"name": "Example Council", "address": {"locality": "Edinburgh"}},
                "tender": {
                    "title": "Facilities contract",
                    "description": "General description mentions Liverpool but is not location evidence",
                    "placeOfPerformance": {
                        "description": "Service delivery at sites in Liverpool and Sefton"
                    },
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertIn("Service delivery at sites in Liverpool and Sefton", records[0]["location_text"])
        self.assertEqual(
            tenderscout.classify_geography(records[0], "Target area")[0], "LOCAL"
        )
        self.assertNotIn("General description mentions Liverpool", records[0]["location_text"])

    def test_award_item_delivery_location_is_extracted(self):
        payload = {
            "releases": [{
                "id": "2026-9880",
                "buyer": {"name": "Liverpool Council", "address": {"locality": "Liverpool"}},
                "tender": {"title": "Cleaning contract", "description": "General text only"},
                "awards": [{
                    "id": "award-1",
                    "items": [{
                        "deliveryLocations": [
                            {"description": "Contract sites in Leeds"}
                        ]
                    }]
                }],
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertIn("Contract sites in Leeds", records[0]["location_text"])
        self.assertEqual(
            tenderscout.classify_geography(records[0], "Target area")[0], "OUT_OF_AREA"
        )

    def test_explicit_out_of_area_performance_site_is_not_local(self):
        payload = {
            "releases": [{
                "id": "2026-9879",
                "buyer": {"name": "Liverpool Council"},
                "tender": {
                    "title": "Regional maintenance contract",
                    "service_location": {"description": "All works delivered in Leeds"},
                },
            }]
        }
        records = tenderscout.extract_records(payload, "Find a Tender")
        self.assertEqual(len(records), 1)
        self.assertIn("All works delivered in Leeds", records[0]["location_text"])
        self.assertEqual(
            tenderscout.classify_geography(records[0], "Target area")[0], "OUT_OF_AREA"
        )


class BearingsDiscoveryTests(unittest.TestCase):
    def test_bearings_import_links_provider_and_never_infers_delivery_location(self):
        payload = {"data": [{
            "id": "opp-test-1",
            "title": "Cleaning services contract",
            "provider": "Test provider",
            "url": "https://provider.example/notice",
            "bearings_url": "https://getbearings.co.uk/o/opp-test-1",
            "kind": "tender",
            "summary": "Commercial cleaning services",
            "conditions": "SMEs may apply",
            "where": "Liverpool City Region",
            "closes": "2099-12-31",
            "first_seen": "2026-10-26",
        }, {
            "id": "opp-test-2", "title": "Grant for businesses",
            "url": "https://provider.example/grant", "kind": "grant",
        }]}
        with patch.object(tenderscout, "get_json", return_value=payload) as mocked:
            stats = {}
            rows = tenderscout.fetch_bearings(5, stats)
        self.assertEqual(len(rows), 1)
        self.assertIn("kind=tender", mocked.call_args.args[0])
        self.assertEqual(rows[0]["notice_url"], "https://provider.example/notice")
        self.assertEqual(rows[0]["location_text"], "")
        self.assertIn("Source credit: Bearings", rows[0]["description"])
        self.assertEqual(
            tenderscout.classify_geography(rows[0], "Target area")[0], "REVIEW"
        )
        self.assertEqual(stats["records"], 1)

    def test_bearings_feed_failure_is_reported_without_crashing(self):
        with patch.object(tenderscout, "get_json", side_effect=RuntimeError("feed offline")):
            stats = {}
            self.assertEqual(tenderscout.fetch_bearings(5, stats), [])
        self.assertIn("feed offline", stats["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
