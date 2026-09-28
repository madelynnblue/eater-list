"""Closure findings: the rules that stop a bad verdict deleting a restaurant."""
import json
import os
import tempfile
import unittest

from e38.closures import load_closed, load_research, write_closed


def write_batch(directory, rows, name="batch_01.out.json"):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh)
    return path


class TestLoadResearch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def load(self, rows):
        write_batch(self.dir, rows)
        return load_research(os.path.join(self.dir, "batch_*.out.json"))

    def test_a_sourced_high_confidence_closure_is_kept(self):
        findings, problems = self.load([
            {"id": "aldea", "status": "closed", "closed_date": "2020-02",
             "source_url": "https://ny.eater.com/x", "evidence": "closing",
             "confidence": "high"},
        ])
        self.assertEqual(findings["aldea"]["status"], "closed")
        self.assertEqual(problems, [])

    def test_a_closed_verdict_without_a_source_is_demoted(self):
        findings, problems = self.load([
            {"id": "mystery", "status": "closed", "source_url": None,
             "evidence": "someone said so", "confidence": "high"},
        ])
        self.assertEqual(findings["mystery"]["status"], "unknown")
        self.assertTrue(any("no source" in p for p in problems))

    def test_a_low_confidence_closure_is_recorded_but_inert(self):
        findings, problems = self.load([
            {"id": "flaming-kitchen", "status": "closed",
             "source_url": "https://wanderlog.com/place/details/398070",
             "evidence": "aggregator listing says closed", "confidence": "low"},
        ])
        self.assertEqual(findings["flaming-kitchen"]["status"], "unknown")
        self.assertTrue(any("low-confidence" in p for p in problems))

    def test_a_bad_status_becomes_unknown(self):
        findings, _ = self.load([{"id": "x", "status": "maybe", "source_url": "https://a.b"}])
        self.assertEqual(findings["x"]["status"], "unknown")

    def test_an_unreadable_batch_is_reported_not_fatal(self):
        with open(os.path.join(self.dir, "batch_09.out.json"), "w") as fh:
            fh.write("{not json")
        findings, problems = load_research(os.path.join(self.dir, "batch_*.out.json"))
        self.assertEqual(findings, {})
        self.assertTrue(problems)

    def test_batches_are_merged(self):
        write_batch(self.dir, [{"id": "a", "status": "open", "source_url": "https://a.b",
                                "evidence": "", "confidence": "high"}], "batch_01.out.json")
        write_batch(self.dir, [{"id": "b", "status": "open", "source_url": "https://a.b",
                                "evidence": "", "confidence": "high"}], "batch_02.out.json")
        findings, _ = load_research(os.path.join(self.dir, "batch_*.out.json"))
        self.assertEqual(set(findings), {"a", "b"})


class TestWriteAndLoadClosed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "closed.json")

    def test_only_closed_places_are_written(self):
        findings = {
            "gone": {"status": "closed", "source_url": "https://x.y", "confidence": "high",
                     "closed_date": "2020-01", "evidence": ""},
            "here": {"status": "open", "source_url": "https://x.y", "confidence": "high",
                     "closed_date": None, "evidence": ""},
            "unclear": {"status": "unknown", "source_url": None, "confidence": "low",
                        "closed_date": None, "evidence": ""},
        }
        doc = write_closed(self.path, findings, total_places=3)
        self.assertEqual(list(doc["places"]), ["gone"])
        self.assertEqual(doc["counts"], {"places_reviewed": 3, "places_on_list": 3,
                                         "closed": 1, "open": 1, "unknown": 1})

    def test_load_closed_ignores_entries_without_a_source(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"places": {
                "good": {"status": "closed", "source_url": "https://x.y"},
                "bad": {"status": "closed", "source_url": None},
                "other": {"status": "open", "source_url": "https://x.y"},
            }}, fh)
        self.assertEqual(list(load_closed(self.path)), ["good"])

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(load_closed(os.path.join(self.dir, "nope.json")), {})


if __name__ == "__main__":
    unittest.main()
