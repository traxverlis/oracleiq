import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from db import store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path_patch = patch.object(store, "DB_PATH", Path(self.temp.name) / "store.db")
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        store.init_db()

    def row(self, **changes):
        return {"sql_id": "sql1", "sql_text": "select * from orders where id=1", "sql_hash": "pattern",
                "source_id": "database1", "schema_name": "APP", "child_number": 0,
                "executions": 100, "elapsed_ms_avg": 1000, **changes}

    def test_identity_preserves_each_cursor(self):
        original = store.upsert_query(self.row())
        for changes in ({"schema_name": "OTHER"}, {"sql_id": "sql2"}, {"child_number": 1}, {"source_id": "database2"}):
            self.assertNotEqual(original, store.upsert_query(self.row(**changes)))
        self.assertEqual(original, store.upsert_query(self.row(executions=200)))
        self.assertEqual(store.get_query_detail(original)["query"]["executions"], 200)

    def test_service_health_expires_and_preserves_success(self):
        self.assertEqual(store.get_service_health()["collector"]["state"], "unknown")
        with patch.object(store.time, "time", return_value=100):
            store.report_service("collector", "waiting", success=True, ttl=30)
            self.assertEqual(store.get_service_health()["collector"]["state"], "waiting")
        with patch.object(store.time, "time", return_value=131):
            status = store.get_service_health()["collector"]
            self.assertEqual(status["state"], "stale")
            self.assertEqual(status["last_success"], 100)
            store.report_service("collector", "error")
            self.assertEqual(store.get_service_health()["collector"]["last_success"], 100)
            store.report_service("collector", "stopped")
            self.assertEqual(store.get_service_health()["collector"]["state"], "stopped")

    def test_raw_samples_are_separate_throttled_and_deleted_with_query(self):
        query_id = store.upsert_query(self.row())
        sample = self.row(cursor_generation="load:address", plan_hash_value=123,
                          elapsed_us=123456, cpu_us=100000, buffer_gets=400, disk_reads=20, rows_processed=10)
        with patch.object(store.time, "time", return_value=100):
            store.upsert_query(sample)
            store.upsert_query(sample)
        with patch.object(store.time, "time", return_value=160):
            store.upsert_query(sample)
        connection = store.get_conn()
        try:
            rows = connection.execute("SELECT * FROM performance_samples WHERE query_id=?", (query_id,)).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["elapsed_us"], 123456)
        finally:
            connection.close()
        store.delete_query_data(query_id)
        connection = store.get_conn()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM performance_samples").fetchone()[0], 0)
        finally:
            connection.close()

    def test_old_performance_samples_expire_on_next_capture(self):
        sample = self.row(cursor_generation="load", plan_hash_value=123, elapsed_us=100,
                          cpu_us=50, buffer_gets=10, disk_reads=1, rows_processed=2)
        with patch.object(store.time, "time", return_value=100):
            query_id = store.upsert_query(sample)
        with patch.object(store.time, "time", return_value=100 + 8 * 86400):
            store.upsert_query(sample)
        connection = store.get_conn()
        try:
            rows = connection.execute("SELECT observed_at FROM performance_samples WHERE query_id=?", (query_id,)).fetchall()
            self.assertEqual([row[0] for row in rows], [100 + 8 * 86400])
        finally:
            connection.close()

    def test_atomic_queue_claim(self):
        query_id = store.upsert_query(self.row())
        self.assertTrue(store.analyzing_queue_add(query_id))
        self.assertFalse(store.analyzing_queue_add(query_id))
        self.assertEqual(store.analyzing_queue_get(), [query_id])
        store.analyzing_queue_remove(query_id)
        self.assertEqual(store.analyzing_queue_get(), [])

    def test_global_search_pagination_and_grouping(self):
        first = store.upsert_query(self.row(sql_text="select " + "column, " * 40 + "needle from orders"))
        store.upsert_query(self.row(sql_id="sql2", elapsed_ms_avg=10, executions=2, module="billing"))
        self.assertEqual(store.get_query_page(search="needle")["items"][0]["id"], first)
        self.assertEqual(store.get_query_page(search="billing")["total"], 1)
        self.assertEqual(store.get_query_page(page_size=1, page=2)["page"], 2)
        grouped = store.get_query_page(group=True)
        self.assertEqual(grouped["total"], 1)
        self.assertEqual(grouped["items"][0]["variant_count"], 2)
        self.assertEqual(grouped["items"][0]["executions"], 102)
        self.assertAlmostEqual(grouped["items"][0]["elapsed_ms_avg"], 100020 / 102)
        self.assertEqual(store.get_query_page(search="' OR 1=1 --")["total"], 0)

    def test_plan_hash_ignores_runtime_statistics(self):
        query_id = store.upsert_query(self.row())
        store.save_plan(query_id, "Plan hash value: 123\nActual rows: 1")
        store.save_plan(query_id, "Plan hash value: 123\nActual rows: 999")
        self.assertEqual(store.get_query_detail(query_id)["query"]["plan_change_detected"], 0)
        store.save_analysis(query_id, {"score": 90, "severity": "ok"})
        store.save_plan(query_id, "Plan hash value: 456")
        detail = store.get_query_detail(query_id)
        self.assertEqual(detail["query"]["plan_change_detected"], 1)
        self.assertEqual(len(store.get_unanalyzed()), 1)

    def test_current_stats_and_complete_deletion(self):
        query_id = store.upsert_query(self.row())
        store.save_analysis(query_id, {"score": 0, "severity": "critical"})
        self.assertEqual(store.get_query_stats()["avg_score"], 0)
        store.save_analysis(query_id, {"score": 90, "severity": "ok"})
        self.assertEqual(store.get_query_stats()["critical"], 0)
        store.chat_add_message(query_id, "user", "synthetic")
        store.delete_query_data(query_id)
        connection = store.get_conn()
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            for table in ("queries", "query_chats", "query_snapshots", "ai_analyses"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()