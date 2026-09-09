import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db import performance, store


class PerformanceTests(unittest.TestCase):
    def sample(self, timestamp, executions, elapsed, **changes):
        return {"observed_at": timestamp, "executions": executions, "elapsed_us": elapsed,
                "cpu_us": elapsed // 2, "buffer_gets": executions * 10, "disk_reads": executions,
                "rows_processed": executions * 2, "cursor_generation": "same", "plan_hash_value": 1, **changes}

    def test_weighted_deltas_not_cumulative_averages(self):
        samples = [self.sample(0, 100, 1000000), self.sample(60, 102, 1200000), self.sample(120, 110, 1600000)]
        result = performance.summarize_period(samples, 0, 180)
        self.assertEqual(result["executions"], 10)
        self.assertEqual(result["elapsed_ms_avg"], 60)
        self.assertEqual(result["coverage_seconds"], 120)

    def test_invalid_intervals_are_excluded(self):
        first = self.sample(0, 100, 1000000)
        cases = [(self.sample(181, 110, 1100000), "gap"),
                 (self.sample(60, 110, 1100000, cursor_generation="new"), "cursor_change"),
                 (self.sample(60, 110, 1100000, plan_hash_value=2), "plan_change"),
                 (self.sample(60, 5, 500), "reset"),
                 (self.sample(60, 100, 1100000), "in_flight")]
        for sample, reason in cases:
            with self.subTest(reason=reason):
                result = performance.summarize_period([first, sample], 0, 900)
                self.assertEqual(result["excluded"][reason], 1)
                self.assertIsNone(result["elapsed_ms_avg"])

    def test_idle_missing_and_boundary_are_not_fabricated(self):
        self.assertEqual(performance.summarize_period([], 0, 60)["state"], "insufficient")
        samples = [self.sample(0, 100, 1000000), self.sample(60, 100, 1000000)]
        self.assertEqual(performance.summarize_period(samples, 0, 60)["state"], "idle")
        self.assertEqual(performance.summarize_period(samples, 1, 61)["intervals"], 0)


class PerformanceStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path_patch = patch.object(store, "DB_PATH", Path(temporary.name) / "performance.db")
        path_patch.start()
        self.addCleanup(path_patch.stop)
        store.init_db()
        self.query_id = store.upsert_query({"sql_id": "fixture", "sql_text": "select 1 from dual", "sql_hash": "fixture"})

    def test_period_query_and_empty_history(self):
        self.assertIsNone(performance.get_performance(-1, 15))
        self.assertEqual(performance.get_performance(self.query_id, 15)["current"]["state"], "insufficient")
        samples = [(0, 10, 100000), (60, 20, 200000), (900, 20, 200000), (960, 30, 400000)]
        connection = store.get_conn()
        try:
            with connection:
                for timestamp, executions, elapsed in samples:
                    connection.execute(
                        "INSERT INTO performance_samples (query_id, observed_at, cursor_generation, plan_hash_value, "
                        "executions, elapsed_us, cpu_us, buffer_gets, disk_reads, rows_processed) VALUES (?,?,?,1,?,?,0,0,0,0)",
                        (self.query_id, timestamp, "fixture", executions, elapsed),
                    )
        finally:
            connection.close()
        result = performance.get_performance(self.query_id, 15, now=1800)
        self.assertEqual(result["previous"]["elapsed_ms_avg"], 10)
        self.assertEqual(result["current"]["elapsed_ms_avg"], 20)
        self.assertEqual(result["variations"]["elapsed_ms_avg"], 100)

    def test_plan_diff_and_cross_query_selection(self):
        self.assertEqual(performance.get_plan_comparison(self.query_id)["plans"], [])
        store.save_plan(self.query_id, "Plan hash value: 1\nTABLE ACCESS FULL", plan_hash_value=1)
        store.save_plan(self.query_id, "Plan hash value: 2\nINDEX RANGE SCAN", plan_hash_value=2)
        result = performance.get_plan_comparison(self.query_id)
        self.assertIn("-TABLE ACCESS FULL", result["diff"])
        self.assertIn("+INDEX RANGE SCAN", result["diff"])
        self.assertFalse(result["truncated"])
        other_id = store.upsert_query({"sql_id": "other", "sql_text": "select 2 from dual", "sql_hash": "other"})
        self.assertIsNone(performance.get_plan_comparison(other_id, result["before"]["id"], result["after"]["id"]))

    def test_plan_comparison_reports_truncation(self):
        store.save_plan(self.query_id, "Plan hash value: 1\n" + "TABLE ACCESS FULL\n" * 1100, plan_hash_value=1)
        store.save_plan(self.query_id, "Plan hash value: 2\nINDEX RANGE SCAN", plan_hash_value=2)
        result = performance.get_plan_comparison(self.query_id)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["before"]["lines"]), 1000)