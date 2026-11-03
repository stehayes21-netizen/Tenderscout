"""Regression tests for artificial-grass-pitch / sports-pitch tender matching on main API."""

import unittest

import tenderscout


class SportsPitchTradeMatchingTests(unittest.TestCase):
    def test_agp_maintenance_notice_matches_grounds_maintenance(self):
        category = tenderscout.classify_trade_category(
            "Maintenance of the artificial grass pitch at a leisure facility. "
            "Leisure AGP maintenance. CPV 77320000: Sports fields maintenance services."
        )
        self.assertEqual(category, "Grounds maintenance and gardening")

    def test_existing_grounds_maintenance_matching_is_preserved(self):
        category = tenderscout.classify_trade_category("Grounds maintenance services")
        self.assertEqual(category, "Grounds maintenance and gardening")

    def test_leisure_centre_management_is_not_pitch_maintenance(self):
        category = tenderscout.classify_trade_category(
            "Management and operation of a leisure centre."
        )
        self.assertNotEqual(category, "Grounds maintenance and gardening")

    def test_sports_equipment_supply_alone_is_not_pitch_maintenance(self):
        category = tenderscout.classify_trade_category("Supply of sports equipment only.")
        self.assertNotEqual(category, "Grounds maintenance and gardening")

    def test_artificial_turf_supply_alone_is_not_pitch_maintenance(self):
        category = tenderscout.classify_trade_category(
            "Supply of artificial turf for a sports facility."
        )
        self.assertNotEqual(category, "Grounds maintenance and gardening")

    def test_synthetic_turf_installation_alone_is_not_pitch_maintenance(self):
        category = tenderscout.classify_trade_category(
            "Installation of synthetic turf at a sports facility."
        )
        self.assertNotEqual(category, "Grounds maintenance and gardening")

    def test_out_of_area_pitch_maintenance_is_not_local(self):
        record = {
            "title": "Artificial grass sports pitch maintenance",
            "description": "Maintenance works at a sports ground in Falkirk.",
            "location_text": "Falkirk, Scotland",
        }
        geography, _reason = tenderscout.classify_geography(record, "Target area")
        self.assertEqual(geography, "OUT_OF_AREA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
