"""Name normalisation, alias resolution and slug stability."""
import unittest

from e38.identity import entry_keys, load_aliases, normalize, resolver, slugify


class TestNormalize(unittest.TestCase):
    def test_case_and_punctuation(self):
        self.assertEqual(normalize("Katz's Delicatessen"), "katz s delicatessen")
        self.assertEqual(normalize("Los Tacos No. 1"), normalize("Los Tacos No.1"))

    def test_accents_are_folded(self):
        self.assertEqual(normalize("Mắm"), "mam")
        self.assertEqual(normalize("Le Veau d’Or"), normalize("Le Veau d'Or"))
        self.assertEqual(normalize("Ernesto’s"), normalize("Ernesto's"))

    def test_ampersand_becomes_and(self):
        self.assertEqual(normalize("A&A Bake"), "a and a bake")


class TestAliases(unittest.TestCase):
    def test_chain_resolution(self):
        canonical = resolver({"a": "b", "b": "c"})
        self.assertEqual(canonical("A"), "c")

    def test_self_reference_terminates(self):
        canonical = resolver({"loop": "loop"})
        self.assertEqual(canonical("Loop"), "loop")

    def test_real_alias_file_is_valid(self):
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "aliases.json")
        aliases = load_aliases(path)
        self.assertIn("the odeon", aliases)
        self.assertTrue(all(isinstance(v, str) for v in aliases.values()))


class TestEntryKeys(unittest.TestCase):
    def test_order_preserved_and_deduplicated(self):
        payload = {"items": [{"name": "Lilia"}, {"name": "Adda"}, {"name": "Lilia"}]}
        self.assertEqual(entry_keys(payload, resolver({})), ["lilia", "adda"])

    def test_alias_merges_two_names(self):
        payload = {"items": [{"name": "The Odeon"}]}
        self.assertEqual(entry_keys(payload, resolver({"the odeon": "odeon"})), ["odeon"])


class TestSlugify(unittest.TestCase):
    def test_stable_ids(self):
        self.assertEqual(slugify("S&P Lunch"), "s-and-p-lunch")
        self.assertEqual(slugify("A&A Bake Doubles and Roti"), "a-and-a-bake-doubles-and-roti")


if __name__ == "__main__":
    unittest.main()
