"""Derivation tests: stretches, membership periods and updates."""
import unittest
from types import SimpleNamespace

from e38.build import Builder


class FakeStore:
    def __init__(self, rows, geo=None):
        self.rows = rows            # list of (ts, payload)
        self.geo = geo or {}

    def get_geo(self, query):
        return self.geo.get(query)

    def captures(self, url):
        return [{"ts": ts, "digest": ts, "mimetype": "text/html", "length": 1}
                for ts, _ in self.rows]

    def get_content(self, url, digest):
        for ts, payload in self.rows:
            if ts == digest:
                return payload
        return None


def obs(ts_list):
    return [(ts, {"items": [{"name": n} for n in names]}) for ts, names in ts_list]


def make_builder(rows, geo=None):
    cfg = SimpleNamespace(aliases_path="/nonexistent", out_dir="/tmp", csv_dir="/tmp")
    return Builder(cfg, FakeStore(rows, geo))


class TestRunsAndPeriods(unittest.TestCase):
    def setUp(self):
        self.builder = make_builder(obs([
            ("20200101", ["Lilia", "Adda"]),
            ("20200201", ["Lilia", "Adda"]),
            ("20200301", ["Lilia", "Kabawa"]),
            ("20200401", ["Lilia"]),
        ]))
        self.data = self.builder.build("u")

    def test_places_counted_once(self):
        self.assertEqual(set(self.data["places"]), {"lilia", "adda", "kabawa"})

    def test_continuous_stretch_is_one_period(self):
        lilia = self.data["places"]["lilia"]
        self.assertEqual(len(lilia["periods"]), 1)
        self.assertEqual(lilia["periods"][0]["from"], "20200101")
        self.assertEqual(lilia["periods"][0]["to"], "20200401")
        self.assertTrue(lilia["on_list_now"])

    def test_place_present_at_first_observation_has_no_add_bracket(self):
        period = self.data["places"]["adda"]["periods"][0]
        self.assertIsNone(period["added_between"])          # already on at coverage start

    def test_rotated_out_place_has_a_removal_bracket(self):
        period = self.data["places"]["adda"]["periods"][0]
        self.assertEqual(period["removed_between"], ["2020-02-01", "2020-03-01"])
        self.assertFalse(self.data["places"]["adda"]["on_list_now"])

    def test_gap_produces_separate_stretches(self):
        builder = make_builder(obs([
            ("20200101", ["Lilia", "Kabawa"]),
            ("20200201", ["Kabawa"]),
            ("20200301", ["Lilia", "Kabawa"]),
        ]))
        data = builder.build("u")
        self.assertEqual(len(data["places"]["lilia"]["periods"]), 2)

    def test_changelog_records_adds_and_removes(self):
        builder = make_builder(obs([
            ("20200101", ["Lilia"]),
            ("20200201", ["Lilia", "Kabawa"]),
        ]))
        changes = builder.build("u")["changes"]
        self.assertEqual(changes[0]["added"], ["Kabawa"])
        self.assertEqual(changes[0]["removed"], [])

    def test_alias_joins_a_rename(self):
        builder = make_builder(obs([
            ("20200101", ["The Odeon"]),
            ("20200201", ["Odeon"]),
        ]))
        builder.canonical = __import__("e38.identity", fromlist=["resolver"]).resolver(
            {"the odeon": "odeon"}
        )
        data = builder.build("u")
        self.assertEqual(list(data["places"]), ["odeon"])
        self.assertEqual(len(data["places"]["odeon"]["periods"]), 1)
        # the literal rename still shows up in the update log
        self.assertEqual(data["changes"][0]["added"], ["Odeon"])
        self.assertEqual(data["changes"][0]["removed"], ["The Odeon"])


class TestGeocodeCache(unittest.TestCase):
    """Geocoded coordinates must survive a rebuild."""

    def test_cached_coordinates_are_applied_when_the_page_had_none(self):
        rows = obs([("20200101", ["Lilia"])])
        rows[0][1]["items"][0]["address"] = "188 Bedford Ave, Brooklyn, NY"
        builder = make_builder(rows, geo={"188 Bedford Ave, Brooklyn, NY":
                                          {"lat": 40.7178, "lng": -73.9571, "source": "nominatim"}})
        place = builder.build("u")["places"]["lilia"]
        self.assertAlmostEqual(place["lat"], 40.7178, places=4)
        self.assertAlmostEqual(place["lng"], -73.9571, places=4)

    def test_a_not_found_marker_does_not_override_a_real_coordinate(self):
        rows = obs([("20200101", ["Lilia"])])
        rows[0][1]["items"][0].update({"address": "nowhere", "lat": 1.5, "lng": 2.5})
        builder = make_builder(rows, geo={"nowhere": {"lat": 0.0, "lng": 0.0}})
        place = builder.build("u")["places"]["lilia"]
        self.assertEqual((place["lat"], place["lng"]), (1.5, 2.5))


class TestLinkSplitting(unittest.TestCase):
    """Eater's own anchor must never be offered as the restaurant's website."""

    def test_eater_url_moves_to_eater_url(self):
        site, eater = Builder._split_links(
            {"url": "https://ny.eater.com/maps/best-new-york-restaurants-38-map#lilia"})
        self.assertEqual(site, "")
        self.assertIn("eater.com", eater)

    def test_real_site_is_kept(self):
        site, eater = Builder._split_links({"url": "http://www.lilianewyork.com/"})
        self.assertEqual(site, "http://www.lilianewyork.com/")
        self.assertEqual(eater, "")

    def test_explicit_website_wins_over_legacy_url(self):
        site, eater = Builder._split_links({
            "website": "https://claudnyc.com",
            "url": "https://ny.eater.com/maps/x#claud-2",
        })
        self.assertEqual(site, "https://claudnyc.com")
        self.assertIn("eater.com", eater)

    def test_eater_url_in_website_field_is_rejected(self):
        site, eater = Builder._split_links({"website": "https://ny.eater.com/x"})
        self.assertEqual(site, "")
        self.assertIn("eater.com", eater)


if __name__ == "__main__":
    unittest.main()
