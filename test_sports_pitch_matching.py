"""Regression tests for artificial-grass-pitch / sports-pitch tender matching."""

import unittest

import tenderscout


class SportsPitchTradeMatchingTests(unittest.TestCase):
    def test_agp_maintenance_notice_matches_a_relevant_trade(self):
        category = tenderscout.classify_trade_category(
            "Maintenance of the artificial grass pitch at a leisure facility.",
            title="Leisure AGP maintenance",
            trade_context="CPV 77320000: Sports fields maintenance services",
        )
        self.assertIn(
            category,
            {"Grounds maintenance and gardening", "Sports pitch maintenance"},
        )

    def test_existing_grounds_maintenance_matching_is_preserved(self):
        category = tenderscout.classify_trade_category(
            "",
            title="Grounds maintenance services",
        )
        self.assertEqual(category, "Grounds maintenance and gardening")

    def test_leisure_centre_management_is_not_misclassified_as_pitch_maintenance(self):
        category = tenderscout.classify_trade_category(
            "Management and operation of a leisure centre.",
            title="Leisure centre management",
        )
        self.assertNotIn(category, {
            "Grounds maintenance and gardening",
            "Sports pitch maintenance",
        })

    def test_sports_equipment_supply_alone_is_not_pitch_maintenance(self):
        category = tenderscout.classify_trade_category(
            "Supply of sports equipment only.",
            title="Sports equipment supply",
        )
        self.assertNotIn(category, {
            "Grounds maintenance and gardening",
            "Sports pitch maintenance",
        })

    def test_out_of_area_pitch_maintenance_is_not_local(self):
        record = {
            "title": "Artificial grass sports pitch maintenance",
            "description": "Maintenance works at a sports ground in Falkirk.",
            "location_text": "Falkirk, Scotland",
        }
        geography, _reason = tenderscout.classify_geography(record, "Target area")
        self.assertEqual(geography, "OUT_OF_AREA")


    def test_artificial_turf_supply_alone_is_not_pitch_maintenance(self):
        category = tenderscout.classify_trade_category(
            "Supply of artificial turf for a sports facility.",
            title="Supply of artificial turf",
        )
        self.assertNotEqual(category, "Grounds maintenance and gardening")

    def test_synthetic_turf_installation_alone_is_not_pitch_maintenance(self):
        category = tenderscout.classify_trade_category(
            "Installation of synthetic turf at a sports facility.",
            title="Synthetic turf installation",
        )
        self.assertNotEqual(category, "Grounds maintenance and gardening")


if __name__ == "__main__":
    unittest.main(verbosity=2)
