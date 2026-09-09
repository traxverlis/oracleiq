import unittest

from analyzer.ai_analyzer import parse_ai_response
from collector.oracle_collector import needs_plan_capture, is_ignored


class AnalysisTests(unittest.TestCase):
    def test_invalid_response_has_no_fabricated_score(self):
        for raw in ("", "Erreur reseau", "SCORE: 12", "SCORE: 101\nSEVERITY: critical\nSUMMARY: Test",
                    '{"score": -1, "severity": "ok", "summary": "Test"}'):
            with self.assertRaises(ValueError):
                parse_ai_response(raw)

    def test_valid_responses(self):
        result = parse_ai_response("SCORE: 0\nSEVERITY: critical\nSUMMARY: Test\n\nDiagnostic")
        self.assertEqual(result["score"], 0)
        result = parse_ai_response('{"score": 90, "severity": "ok", "summary": "Valide"}')
        self.assertEqual(result["score"], 90)

    def test_recapture_and_business_jdbc_queries(self):
        self.assertTrue(needs_plan_capture(None, 123))
        self.assertTrue(needs_plan_capture((123,), 456))
        self.assertFalse(needs_plan_capture((123,), 123))
        self.assertFalse(is_ignored("select * from orders", "APP", "JDBC Thin Client"))


if __name__ == "__main__":
    unittest.main()