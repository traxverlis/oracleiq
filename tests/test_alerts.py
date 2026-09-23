import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from db import alerts, store


class AlertTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".test-alerts-")
        self.addCleanup(directory.cleanup)
        path_patch = patch.object(store, "DB_PATH", Path(directory.name) / "alerts.db")
        path_patch.start()
        self.addCleanup(path_patch.stop)
        store.init_db()
        self.query_id = store.upsert_query(
            {"sql_id": "fixture", "sql_text": "select 1 from dual", "sql_hash": "fixture"}
        )

    def measurements(self, *, baseline=200, current=300, executions=10, coverage=450, now=1800):
        connection = store.get_conn()
        try:
            with connection:
                connection.execute("DELETE FROM performance_samples")
                connection.execute("DELETE FROM settings WHERE key='alerts_last_regression_evaluation'")
                for period, latency in enumerate((baseline, current)):
                    for index, offset in enumerate((0, 150, 300, coverage)):
                        count = executions * index // 3
                        elapsed = count * latency * 1000 + (executions * baseline * 1000 if period else 0)
                        connection.execute(
                            "INSERT INTO performance_samples(query_id,observed_at,cursor_generation,plan_hash_value,"
                            "executions,elapsed_us,cpu_us,buffer_gets,disk_reads,rows_processed) "
                            "VALUES(?,?,'fixture',1,?,?,0,0,0,0)",
                            (self.query_id, now - 1800 + period * 900 + offset,
                             count + period * executions, round(elapsed)),
                        )
        finally:
            connection.close()

    def report(self, name, state, *, now=100, ttl=30):
        with patch.object(store.time, "time", return_value=now):
            store.report_service(name, state, ttl=ttl)

    def test_absent_measurements_and_unobserved_services_are_not_alerts(self):
        store.set_settings({"analyzer_mode": "auto", "collector_active": "true"})
        alerts.evaluate_alerts(now=1800)
        self.assertEqual(alerts.list_alerts(), {"items": [], "total": 0, "unacknowledged": 0})

    def test_exact_regression_thresholds_trigger_warning(self):
        self.measurements()
        alerts.evaluate_alerts(now=1800)
        item = alerts.list_alerts()["items"][0]
        self.assertEqual(item["kind"], "regression")
        self.assertEqual(item["severity"], "warning")
        self.assertEqual(item["query_id"], self.query_id)
        self.assertIn("pas une preuve", item["message"])
        self.assertIsNone(item["resolved_at"])

    def test_below_any_threshold_is_not_regression(self):
        cases = ({"executions": 9}, {"coverage": 449},
                 {"baseline": 201, "current": 301}, {"baseline": 199, "current": 298.5},
                 {"baseline": 0, "current": 100}, {"current": 100})
        for changes in cases:
            with self.subTest(changes=changes):
                self.measurements(**changes)
                alerts.evaluate_alerts(now=1800)
                self.assertEqual(alerts.list_alerts()["total"], 0)

    def test_cumulative_query_latency_alone_does_not_trigger_alert(self):
        store.upsert_query({"sql_id": "fixture", "sql_text": "select 1 from dual", "sql_hash": "fixture",
                            "elapsed_ms_avg": 999999, "executions": 1000000})
        alerts.evaluate_alerts(now=1800)
        self.assertEqual(alerts.list_alerts()["total"], 0)

    def test_invalid_measurement_intervals_cannot_trigger_regression(self):
        self.measurements()
        connection = store.get_conn()
        try:
            with connection:
                connection.execute("UPDATE performance_samples SET cursor_generation=CAST(id AS TEXT)")
        finally:
            connection.close()
        alerts.evaluate_alerts(now=1800)
        self.assertEqual(alerts.list_alerts()["total"], 0)

    def test_acknowledgement_deduplication_recovery_and_relapse(self):
        self.measurements()
        alerts.evaluate_alerts(now=1800)
        first = alerts.list_alerts()["items"][0]
        self.assertTrue(alerts.acknowledge_alert(first["id"]))
        acknowledged = alerts.list_alerts()["items"][0]["acknowledged_at"]
        self.assertTrue(alerts.acknowledge_alert(first["id"]))
        alerts.evaluate_alerts(now=1800)
        same = alerts.list_alerts()["items"][0]
        self.assertEqual(same["id"], first["id"])
        self.assertEqual(same["acknowledged_at"], acknowledged)
        self.assertIsNone(same["resolved_at"])
        self.assertEqual(alerts.list_alerts()["unacknowledged"], 0)

        self.measurements(current=200, now=3600)
        alerts.evaluate_alerts(now=3600)
        self.assertIsNotNone(alerts.list_alerts()["items"][0]["resolved_at"])
        self.measurements(now=5400)
        alerts.evaluate_alerts(now=5400)
        items = alerts.list_alerts()["items"]
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0]["id"], first["id"])
        self.assertIsNone(items[0]["acknowledged_at"])
        self.assertIsNone(items[0]["resolved_at"])

    def test_missing_measurements_do_not_fabricate_recovery(self):
        self.measurements()
        alerts.evaluate_alerts(now=1800)
        alerts.evaluate_alerts(now=10000)
        self.assertIsNone(alerts.list_alerts()["items"][0]["resolved_at"])

    def test_regression_scan_is_throttled_persistently_but_service_checks_continue(self):
        self.measurements()
        with patch.object(alerts, "summarize_period", wraps=alerts.summarize_period) as summarize:
            alerts.evaluate_alerts(now=1800)
            self.assertEqual(summarize.call_count, 2)
            self.report("collector", "error", now=1801)
            alerts.evaluate_alerts(now=1801)
            alerts.evaluate_alerts(now=1859)
            self.assertEqual(summarize.call_count, 2)
            self.assertEqual(alerts.list_alerts()["total"], 2)
            alerts.evaluate_alerts(now=1860)
            self.assertEqual(summarize.call_count, 4)

    def test_concurrent_evaluators_create_one_incident(self):
        self.measurements()
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(alerts.evaluate_alerts, [1800] * 4))
        self.assertEqual(alerts.list_alerts()["total"], 1)

    def test_service_error_and_stale_signal_share_incident_until_recovery(self):
        self.report("collector", "waiting")
        alerts.evaluate_alerts(now=130)
        self.assertEqual(alerts.list_alerts()["total"], 0)
        alerts.evaluate_alerts(now=131)
        first = alerts.list_alerts()["items"][0]
        self.assertEqual(first["severity"], "warning")
        self.report("collector", "error", now=132)
        alerts.evaluate_alerts(now=132)
        self.assertEqual(alerts.list_alerts()["items"][0]["id"], first["id"])
        self.assertEqual(alerts.list_alerts()["items"][0]["severity"], "critical")
        self.report("collector", "waiting", now=133)
        alerts.evaluate_alerts(now=133)
        self.assertIsNotNone(alerts.list_alerts()["items"][0]["resolved_at"])
        self.report("collector", "error", now=134)
        alerts.evaluate_alerts(now=134)
        self.assertEqual(alerts.list_alerts()["total"], 2)

    def test_disabled_collector_and_manual_analyzer_are_not_outages(self):
        self.report("collector", "error")
        self.report("analyzer", "error")
        store.set_settings({"collector_active": "false", "analyzer_mode": "manual"})
        alerts.evaluate_alerts(now=1000)
        self.assertEqual(alerts.list_alerts()["total"], 0)
        store.set_settings({"collector_active": "true", "analyzer_mode": "auto"})
        alerts.evaluate_alerts(now=1001)
        self.assertEqual(alerts.list_alerts()["total"], 2)
        store.set_settings({"collector_active": "false", "analyzer_mode": "manual"})
        alerts.evaluate_alerts(now=1002)
        self.assertTrue(all(item["resolved_at"] for item in alerts.list_alerts()["items"]))

    def test_stopped_enabled_services_alert_immediately_and_deduplicate(self):
        store.set_setting("analyzer_mode", "auto")
        self.report("collector", "stopped")
        self.report("analyzer", "stopped")
        alerts.evaluate_alerts(now=100)
        alerts.evaluate_alerts(now=131)
        result = alerts.list_alerts()
        self.assertEqual(result["total"], 2)
        self.assertTrue(all(item["severity"] == "critical" and item["resolved_at"] is None
                            for item in result["items"]))
        store.set_settings({"collector_active": "false", "analyzer_mode": "manual"})
        alerts.evaluate_alerts(now=132)
        self.assertTrue(all(item["resolved_at"] for item in alerts.list_alerts()["items"]))

    def test_reactivated_paused_and_manual_services_wait_until_heartbeat_expires(self):
        store.set_settings({"collector_active": "false", "analyzer_mode": "manual"})
        self.report("collector", "paused")
        self.report("analyzer", "manual")
        alerts.evaluate_alerts(now=100)
        self.assertEqual(alerts.list_alerts()["total"], 0)
        store.set_settings({"collector_active": "true", "analyzer_mode": "auto"})
        alerts.evaluate_alerts(now=120)
        alerts.evaluate_alerts(now=130)
        self.assertEqual(alerts.list_alerts()["total"], 0)
        alerts.evaluate_alerts(now=131)
        result = alerts.list_alerts()
        self.assertEqual(result["total"], 2)
        self.assertTrue(all(item["severity"] == "warning" for item in result["items"]))
        self.report("collector", "waiting", now=132)
        self.report("analyzer", "waiting", now=132)
        alerts.evaluate_alerts(now=132)
        self.assertTrue(all(item["resolved_at"] for item in alerts.list_alerts()["items"]))

    def test_disabled_services_stay_quiet_even_when_stop_or_pause_signal_is_expired(self):
        store.set_settings({"collector_active": "false", "analyzer_mode": "manual"})
        for state in ("stopped", "paused", "manual"):
            self.report("collector", state)
            self.report("analyzer", state)
            alerts.evaluate_alerts(now=1000)
            self.assertEqual(alerts.list_alerts()["total"], 0)

    def test_regression_scan_uses_global_time_index(self):
        connection = store.get_conn()
        try:
            columns = connection.execute("PRAGMA index_info(idx_performance_time)").fetchall()
            self.assertEqual([row["name"] for row in columns], ["observed_at"])
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM performance_samples WHERE observed_at>=? AND observed_at<=? "
                "ORDER BY query_id,observed_at,id", (0, 1800),
            ).fetchall()
            self.assertTrue(any("SEARCH performance_samples USING INDEX idx_performance_time" in row["detail"]
                                for row in plan), [dict(row) for row in plan])
        finally:
            connection.close()

    def test_acknowledgement_filter_pagination_and_counts(self):
        self.report("collector", "error")
        self.report("analyzer", "error")
        store.set_setting("analyzer_mode", "auto")
        alerts.evaluate_alerts(now=100)
        result = alerts.list_alerts(limit=1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["unacknowledged"], 2)
        self.assertTrue(alerts.acknowledge_alert(result["items"][0]["id"]))
        filtered = alerts.list_alerts(include_acknowledged=False)
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["unacknowledged"], 1)
        self.assertEqual(alerts.list_alerts(limit=1, offset=1)["items"], filtered["items"])
        self.assertFalse(alerts.acknowledge_alert(-1))
        self.assertEqual(alerts.list_alerts(offset=999)["items"], [])

    def test_history_survives_query_deletion_and_reinitialization(self):
        self.measurements()
        alerts.evaluate_alerts(now=1800)
        store.delete_query_data(self.query_id)
        store.init_db()
        alerts.evaluate_alerts(now=1860)
        result = alerts.list_alerts()
        self.assertEqual(result["total"], 1)
        self.assertIsNone(result["items"][0]["query_id"])
        self.assertIsNotNone(result["items"][0]["resolved_at"])
        self.assertEqual(result["unacknowledged"], 1)


if __name__ == "__main__":
    unittest.main()
