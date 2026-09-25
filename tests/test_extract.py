"""Extractor tests against real archived pages (gzipped fixtures)."""
import gzip
import os
import unittest

from e38.extract import extract

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with gzip.open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read().decode("utf-8", "replace")


class TestModernPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract(load("eater38-2026-nextjs.html.gz"))

    def test_uses_embedded_next_data(self):
        self.assertEqual(self.result["source"], "next_data")
        self.assertGreaterEqual(self.result["n"], 38)

    def test_known_entry_is_first(self):
        self.assertEqual(self.result["items"][0]["name"], "La Piraña Lechonera")

    def test_coordinates_and_address_are_captured(self):
        entry = self.result["items"][0]
        self.assertAlmostEqual(entry["lat"], 40.815576, places=5)
        self.assertAlmostEqual(entry["lng"], -73.90649, places=5)
        self.assertIn("Bronx", entry["address"])

    def test_labelled_copy_is_parsed(self):
        entry = self.result["items"][0]
        self.assertTrue(entry.get("open_for") or entry.get("price"))
        self.assertTrue(all(i["name"] for i in self.result["items"]))


class TestLegacyPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = extract(load("eater38-2017-mapstack.html.gz"))

    def test_extracts_38_restaurants(self):
        self.assertEqual(self.result["n"], 38)

    def test_first_entry(self):
        self.assertEqual(self.result["items"][0]["name"], "The Odeon")
        self.assertEqual(self.result["items"][0]["slug"], "the-odeon-2")

    def test_address_is_merged_in_from_the_card_markup(self):
        self.assertIn("Broadway", self.result["items"][0]["address"])

    def test_restaurant_website_is_taken_from_the_visit_website_link(self):
        self.assertEqual(self.result["items"][0]["website"],
                         "http://www.theodeonrestaurant.com")

    def test_coordinates_come_from_the_directions_link(self):
        entry = self.result["items"][0]
        self.assertAlmostEqual(entry["lat"], 40.71700358931347, places=6)
        self.assertAlmostEqual(entry["lng"], -74.00810144732247, places=6)

    def test_eater_anchor_is_not_mistaken_for_a_website(self):
        entry = self.result["items"][0]
        self.assertNotIn("eater.com", entry["website"])
        self.assertTrue(entry["eater_url"].startswith("https://ny.eater.com/"))

    def test_intro_card_is_not_an_entry(self):
        names = [i["name"] for i in self.result["items"]]
        self.assertNotIn("intro", [i["slug"] for i in self.result["items"]])
        self.assertTrue(all(names))


if __name__ == "__main__":
    unittest.main()
