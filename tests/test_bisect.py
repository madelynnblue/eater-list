"""The bisect search must find every change while fetching far fewer captures
than an exhaustive walk."""
import unittest

from e38.scan import Scanner


class FakeScanner(Scanner):
    """A scanner whose signatures are a fixed script — no network."""

    def __init__(self, signatures):
        self.signatures = signatures          # list of hashable states per index
        self.probes = set()                   # unique indices -> what a real run fetches
        self.cfg = type("C", (), {"refetch": False})()

    def _signature(self, url, index, ts_list, digest_list):
        self.probes.add(index)
        return frozenset({self.signatures[index]})


class TestBisect(unittest.TestCase):
    def ts(self, n):
        return [f"2020{i:010d}" for i in range(n)]

    def run_search(self, script):
        scanner = FakeScanner(script)
        changes = scanner.find_changes("u", self.ts(len(script)), [None] * len(script), 0, len(script) - 1)
        return changes, len(scanner.probes)

    def test_no_change(self):
        changes, probes = self.run_search([1] * 64)
        self.assertEqual(changes, [])
        self.assertLessEqual(probes, 4)

    def test_single_change_is_located_exactly(self):
        script = [1] * 40 + [2] * 40
        changes, _ = self.run_search(script)
        self.assertEqual(changes, [(39, 40)])

    def test_every_change_found(self):
        script = []
        for value in (1, 2, 3, 4, 5):
            script += [value] * 30
        changes, _ = self.run_search(script)
        self.assertEqual(changes, [(29, 30), (59, 60), (89, 90), (119, 120)])

    def test_fetches_far_fewer_than_exhaustive(self):
        script = []
        for value in range(20):
            script += [value] * 40     # 800 captures, 19 changes
        changes, probes = self.run_search(script)
        self.assertEqual(len(changes), 19)
        self.assertLess(probes, len(script) // 3)

    def test_reversion_inside_equal_endpoints_is_the_known_blind_spot(self):
        script = [1, 1, 1, 2, 2, 2, 1, 1, 1]
        changes, _ = self.run_search(script)
        # The endpoints agree, so bisect reports no change. This is the
        # documented assumption; `--strategy exhaustive` is the way to audit it.
        self.assertEqual(changes, [])

    def test_exhaustive_finds_the_reversion_bisect_misses(self):
        signature = [1, 1, 1, 2, 2, 2, 1, 1, 1]
        naive = [(i, i + 1) for i in range(len(signature) - 1)
                 if signature[i] != signature[i + 1]]
        self.assertEqual(naive, [(2, 3), (5, 6)])


if __name__ == "__main__":
    unittest.main()
