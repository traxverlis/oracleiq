import sqlite3
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from collector import connection
from collector import oracle_collector as collector


PLAN = """SQL_ID fixture, child number 0
Plan hash value: 123
-------------------------------------
| Id | Operation         | Name     |
-------------------------------------
|  0 | SELECT STATEMENT  |          |
|* 1 | TABLE ACCESS FULL | ORDERS   |
-------------------------------------
"""
DIAGNOSTIC = "SQL_ID fixture\nPlan hash value: 123\nNOTE: cannot fetch plan for SQL_ID: fixture"
SETTINGS = {"oracle_dsn": "source-A", "oracle_user": "fixture", "oracle_password": "fixture"}


class CollectorReliabilityTests(unittest.TestCase):
    def setUp(self):
        for target in ("collector.connection.oracledb.connect", "db.store.get_conn"):
            guard = patch(target, side_effect=AssertionError("External resources forbidden in this test"))
            guard.start()
            self.addCleanup(guard.stop)

    def query(self, **changes):
        return {"source_id": "source-A", "sql_id": "fixture", "sql_hash": "fixture",
                "sql_text": "select * from orders", "plan_hash_value": 123,
                "child_number": 0, "cursor_generation": "generation-1", **changes}

    def database(self, last_plan=None):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = last_plan
        return db

    def test_source_identity_requires_nonempty_exact_actual_dsn(self):
        for source, actual in (
            ("", "source-A"), ("source-A", ""), ("source-A", "source-B"),
            ("source-A", "SOURCE-A"), ("alias", "host:1521/service"),
            ("host:1521/service", "host:1521/other"), (None, "source-A"),
        ):
            with self.subTest(source=source, actual=actual), self.assertRaises(ValueError):
                connection.assert_query_source({"source_id": source}, SimpleNamespace(dsn=actual))
        connection.assert_query_source({"source_id": " source-A "}, SimpleNamespace(dsn="source-A "))
        with self.assertRaises(ValueError):
            connection.assert_query_source({}, SimpleNamespace())

    def test_source_identity_accepts_sqlite_rows(self):
        with sqlite3.connect(":memory:") as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT 'source-A' AS source_id").fetchone()
            connection.assert_query_source(row, SimpleNamespace(dsn="source-A"))
        db.close()

    def test_settings_are_one_atomic_snapshot(self):
        with patch("config.ORACLE_DSN", "default-source"), patch("config.ORACLE_USER", "default-user"), \
                patch("config.ORACLE_PASSWORD", "default-password"), \
                patch("db.store.get_settings", return_value=SETTINGS.copy()) as snapshot, \
                patch("db.store.get_setting", side_effect=AssertionError("Non-atomic settings read")):
            self.assertEqual(connection.get_oracle_settings(), SETTINGS)
        snapshot.assert_called_once_with({
            "oracle_dsn": "default-source", "oracle_user": "default-user",
            "oracle_password": "default-password",
        })

    def test_connection_configuration_failure_closes_connection(self):
        oracle = MagicMock()
        with patch("collector.connection.oracledb.connect", return_value=oracle), \
                patch.dict("os.environ", {"ODIN_ORACLE_TIMEOUT_MS": "invalid"}), \
                self.assertRaises(ValueError):
            connection.connect_oracle(user="fixture", password="fixture", dsn="source-A")
        oracle.close.assert_called_once()

    def test_plan_validator_rejects_diagnostics_and_incomplete_output(self):
        for text in (None, "", "[Plan non disponible: error]", DIAGNOSTIC,
                     "ERROR: cannot find plan", "ORA-01031: insufficient privileges",
                     "Plan hash value: 123", "| Id | Operation |", "no rows selected"):
            with self.subTest(text=text):
                self.assertFalse(connection.is_execution_plan_available(text))
        self.assertTrue(connection.is_execution_plan_available(PLAN))
        self.assertTrue(connection.is_execution_plan_available(
            "select 'cannot fetch plan' from dual\n" + PLAN
        ))

    def test_same_hash_with_diagnostic_still_needs_capture(self):
        self.assertTrue(collector.needs_plan_capture((123, DIAGNOSTIC), 123))
        self.assertFalse(collector.needs_plan_capture((123, PLAN), 123))
        self.assertTrue(collector.needs_plan_capture((123, PLAN), 456))
        self.assertFalse(collector.needs_plan_capture((123,), 123))

    def test_plan_fetch_closes_cursor_and_rejects_diagnostic(self):
        oracle = MagicMock()
        oracle.cursor.return_value.fetchall.return_value = [(DIAGNOSTIC,)]
        self.assertEqual(collector.get_execution_plan(oracle, "fixture"), "")
        oracle.cursor.return_value.close.assert_called_once()

    def test_plan_fetch_closes_cursor_on_failure(self):
        oracle = MagicMock()
        oracle.cursor.return_value.execute.side_effect = RuntimeError("fixture")
        self.assertEqual(collector.get_execution_plan(oracle, "fixture"), "")
        oracle.cursor.return_value.close.assert_called_once()

    def test_plan_fetch_reads_clob_before_closing(self):
        oracle = MagicMock()
        clob = MagicMock()
        clob.read.return_value = PLAN
        oracle.cursor.return_value.fetchall.return_value = [(clob,)]
        self.assertEqual(collector.get_execution_plan(oracle, "fixture"), PLAN)
        clob.read.assert_called_once()
        oracle.cursor.return_value.close.assert_called_once()

    def test_bind_fetch_closes_on_success_and_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                oracle = MagicMock()
                oracle.cursor.return_value.description = [("BIND_NAME",), ("VALUE",)]
                oracle.cursor.return_value.fetchall.return_value = [(":id", "1")]
                if fails:
                    oracle.cursor.return_value.execute.side_effect = RuntimeError("fixture")
                result = collector.fetch_bind_values(oracle, "fixture")
                self.assertEqual(result, [] if fails else [{"bind_name": ":id", "value": "1"}])
                oracle.cursor.return_value.close.assert_called_once()

    def test_bind_fetch_filters_the_captured_child(self):
        oracle = MagicMock()
        oracle.cursor.return_value.description = []
        oracle.cursor.return_value.fetchall.return_value = []
        collector.fetch_bind_values(oracle, "fixture", child_number=7)
        self.assertEqual(oracle.cursor.return_value.execute.call_args.kwargs,
                         {"sql_id": "fixture", "child_no": 7})

    def test_full_text_closes_both_failed_and_fallback_cursors(self):
        oracle = MagicMock()
        first, second = MagicMock(), MagicMock()
        first.execute.side_effect = RuntimeError("fixture")
        second.fetchone.return_value = ("select * from orders",)
        oracle.cursor.side_effect = [first, second]
        self.assertEqual(collector.get_full_sql_text(oracle, "fixture"), "select * from orders")
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_poll_closes_cursor_on_success_and_failure(self):
        for fails in (False, True):
            with self.subTest(fails=fails):
                oracle = MagicMock(dsn="source-A")
                oracle.cursor.return_value.description = []
                oracle.cursor.return_value.fetchall.return_value = []
                if fails:
                    oracle.cursor.return_value.execute.side_effect = RuntimeError("fixture")
                    with self.assertRaises(RuntimeError):
                        collector.poll_vsql(oracle)
                else:
                    self.assertEqual(collector.poll_vsql(oracle), [])
                oracle.cursor.return_value.close.assert_called_once()

    def test_poll_never_falls_back_to_config_source(self):
        oracle = MagicMock(dsn="")
        with self.assertRaises(ValueError):
            collector.poll_vsql(oracle)
        oracle.cursor.assert_not_called()

    def test_poll_records_the_actual_connection_source(self):
        oracle = MagicMock(dsn=" actual-source ")
        oracle.cursor.return_value.description = [
            ("SQL_ID",), ("SQL_TEXT",), ("PARSING_SCHEMA_NAME",), ("MODULE",),
        ]
        oracle.cursor.return_value.fetchall.return_value = [
            ("fixture", "select * from orders", "APP", "business"),
        ]
        rows = collector.poll_vsql(oracle)
        self.assertEqual(rows[0]["source_id"], "actual-source")

    def test_backfill_empty_closes_sqlite_connection_and_cursor(self):
        db = self.database()
        db.execute.return_value.fetchall.return_value = []
        oracle = MagicMock(dsn="source-A")
        with patch("db.store.get_conn", return_value=db):
            collector.backfill_schema_names(oracle)
        db.close.assert_called_once()
        db.execute.return_value.close.assert_called_once()
        oracle.cursor.assert_not_called()

    def test_backfill_sqlite_exception_closes_connection(self):
        db = self.database()
        db.execute.side_effect = RuntimeError("fixture")
        with patch("db.store.get_conn", return_value=db), self.assertRaises(RuntimeError):
            collector.backfill_schema_names(MagicMock(dsn="source-A"))
        db.close.assert_called_once()

    def test_backfill_oracle_exception_closes_all_resources(self):
        db = self.database()
        db.execute.return_value.fetchall.return_value = [(1, "fixture", 2, "source-A")]
        oracle = MagicMock(dsn="source-A")
        oracle.cursor.return_value.execute.side_effect = RuntimeError("fixture")
        with patch("db.store.get_conn", return_value=db), self.assertRaises(RuntimeError):
            collector.backfill_schema_names(oracle)
        oracle.cursor.return_value.close.assert_called_once()
        db.close.assert_called_once()
        db.execute.return_value.close.assert_called_once()

    def test_backfill_rejects_different_source_before_oracle(self):
        db = self.database()
        db.execute.return_value.fetchall.return_value = [(1, "fixture", 2, "source-B")]
        oracle = MagicMock(dsn="source-A")
        with patch("db.store.get_conn", return_value=db), self.assertRaises(ValueError):
            collector.backfill_schema_names(oracle)
        oracle.cursor.assert_not_called()
        db.close.assert_called_once()

    def test_detail_refresh_rejects_other_source_before_any_db_access(self):
        with self.assertRaises(ValueError):
            collector.refresh_query_details(SimpleNamespace(dsn="source-B"), self.query(), 1, {}, 0)

    def test_diagnostic_plan_retries_same_hash_after_backoff(self):
        db = self.database((123, DIAGNOSTIC, "2026-01-01 00:00:00"))
        state = {}
        with patch("db.store.get_conn", return_value=db), \
                patch.object(collector, "get_execution_plan", side_effect=[DIAGNOSTIC, PLAN]) as fetch, \
                patch.object(collector, "fetch_bind_values", return_value=[]) as binds, \
                patch.object(collector, "save_plan") as save:
            outcomes = [collector.refresh_query_details(
                SimpleNamespace(dsn="source-A"), self.query(), 1, state, now
            ) for now in (0, 5, 29, 30, 35)]
        self.assertEqual(outcomes, [False, False, False, True, False])
        self.assertEqual(fetch.call_count, 2)
        binds.assert_called_once()
        save.assert_called_once_with(1, PLAN, plan_hash_value=123)
        db.close.assert_called_once()
        db.execute.return_value.close.assert_called_once()

    def test_stable_hash_refreshes_binds_and_runtime_plan_on_separate_ttls(self):
        state = {}
        with patch("db.store.get_conn", return_value=self.database()), \
                patch.object(collector, "get_execution_plan", return_value=PLAN) as plans, \
                patch.object(collector, "fetch_bind_values", return_value=[]) as binds, \
                patch.object(collector, "save_plan") as save:
            for now in range(0, 301, 5):
                collector.refresh_query_details(SimpleNamespace(dsn="source-A"), self.query(), 1, state, now)
        self.assertEqual(plans.call_count, 2)
        self.assertEqual(save.call_count, 2)
        self.assertEqual(binds.call_count, 6)

    def test_hash_change_captures_immediately_despite_ttl(self):
        state = {}
        with patch("db.store.get_conn", return_value=self.database()), \
                patch.object(collector, "get_execution_plan",
                             side_effect=[PLAN, PLAN.replace("value: 123", "value: 456")]) as plans, \
                patch.object(collector, "fetch_bind_values", return_value=[]) as binds, \
                patch.object(collector, "save_plan") as save:
            for now, value in ((0, 123), (5, 456)):
                collector.refresh_query_details(
                    SimpleNamespace(dsn="source-A"), self.query(plan_hash_value=value), 1, state, now)
        self.assertEqual(plans.call_count, 2)
        self.assertEqual(binds.call_count, 2)
        self.assertEqual(save.call_args.kwargs["plan_hash_value"], 456)

    def test_plan_race_with_different_hash_is_not_saved(self):
        state = {}
        with patch("db.store.get_conn", return_value=self.database()), \
                patch.object(collector, "get_execution_plan",
                             side_effect=[PLAN.replace("value: 123", "value: 456"), PLAN]) as plans, \
                patch.object(collector, "fetch_bind_values", return_value=[]), \
                patch.object(collector, "save_plan") as save:
            for now in (0, 5, 30):
                collector.refresh_query_details(SimpleNamespace(dsn="source-A"), self.query(), 1, state, now)
        self.assertEqual(plans.call_count, 2)
        save.assert_called_once_with(1, PLAN, plan_hash_value=123)

    def test_recent_persisted_plan_avoids_restart_recapture(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        state = {}
        with patch("db.store.get_conn", return_value=self.database((123, PLAN, timestamp))), \
                patch.object(collector, "get_execution_plan", return_value=PLAN) as plans, \
                patch.object(collector, "fetch_bind_values", return_value=[]), \
                patch.object(collector, "save_plan"):
            collector.refresh_query_details(SimpleNamespace(dsn="source-A"), self.query(), 1, state, 0)
            plans.assert_not_called()
            collector.refresh_query_details(SimpleNamespace(dsn="source-A"), self.query(), 1, state, 301)
            plans.assert_called_once()

    def test_failed_runtime_refresh_does_not_mark_old_plan_fresh(self):
        state = {"plan_key": (123, 0, "generation-1"), "plan_success": 0}
        with patch.object(collector, "get_execution_plan", side_effect=["", PLAN]) as plans, \
                patch.object(collector, "fetch_bind_values", return_value=[]), \
                patch.object(collector, "save_plan") as save:
            collector.refresh_query_details(SimpleNamespace(dsn="source-A"), self.query(), 1, state, 300)
            self.assertEqual(state["plan_success"], 0)
            collector.refresh_query_details(SimpleNamespace(dsn="source-A"), self.query(), 1, state, 305)
            collector.refresh_query_details(SimpleNamespace(dsn="source-A"), self.query(), 1, state, 330)
        self.assertEqual(state["plan_success"], 330)
        self.assertEqual(plans.call_count, 2)
        save.assert_called_once()

    def test_detail_sqlite_failure_closes_connection(self):
        db = self.database()
        db.execute.side_effect = RuntimeError("fixture")
        with patch("db.store.get_conn", return_value=db), self.assertRaises(RuntimeError):
            collector.refresh_query_details(SimpleNamespace(dsn="source-A"), self.query(), 1, {}, 0)
        db.close.assert_called_once()

    def run_two_iterations(self, snapshots, connections, polls):
        with patch.object(collector, "get_oracle_settings", side_effect=snapshots), \
                patch.object(collector, "connect_oracle", side_effect=connections) as connect, \
                patch.object(collector, "poll_vsql", side_effect=polls) as poll, \
                patch.object(collector, "backfill_schema_names"), \
                patch.object(collector.logger, "exception", side_effect=OSError("Logger unavailable")), \
                patch.object(collector.console, "print"), patch.object(collector.console, "rule"), \
                patch("db.store.get_setting", side_effect=lambda key, default="": default), \
                patch("db.store.report_service"), \
                patch.object(collector.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
            collector._run_collector()
        return connect, poll, sleep

    def test_logging_failure_cannot_break_recovery(self):
        first, second = MagicMock(dsn="source-A"), MagicMock(dsn="source-A")
        connect, poll, sleep = self.run_two_iterations(
            [SETTINGS.copy(), SETTINGS.copy()], [first, second], [RuntimeError("poll failure"), []],
        )
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(poll.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(10), call(collector.POLL_INTERVAL_SEC)])
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_initial_connection_failure_retries(self):
        oracle = MagicMock(dsn="source-A")
        connect, poll, sleep = self.run_two_iterations(
            [SETTINGS.copy(), SETTINGS.copy()], [RuntimeError("connect failure"), oracle], [[]],
        )
        self.assertEqual(connect.call_count, 2)
        poll.assert_called_once_with(oracle)
        self.assertEqual(sleep.call_args_list, [call(10), call(collector.POLL_INTERVAL_SEC)])
        oracle.close.assert_called_once()

    def test_changed_settings_reconnect_as_a_coherent_set(self):
        changed = {"oracle_dsn": "source-B", "oracle_user": "other-user", "oracle_password": "other"}
        first, second = MagicMock(dsn="source-A"), MagicMock(dsn="source-B")
        connect, poll, _ = self.run_two_iterations(
            [SETTINGS.copy(), changed], [first, second], [[], []],
        )
        self.assertEqual(connect.call_args_list, [
            call(user="fixture", password="fixture", dsn="source-A"),
            call(user="other-user", password="other", dsn="source-B"),
        ])
        self.assertEqual(poll.call_args_list, [call(first), call(second)])
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_password_change_alone_reconnects(self):
        changed = {**SETTINGS, "oracle_password": "changed"}
        first, second = MagicMock(dsn="source-A"), MagicMock(dsn="source-A")
        connect, _, _ = self.run_two_iterations(
            [SETTINGS.copy(), changed], [first, second], [[], []],
        )
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(connect.call_args.kwargs["password"], "changed")
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_short_poll_absence_does_not_reset_capture_ttls(self):
        oracle = MagicMock(dsn="source-A")
        with patch.object(collector, "get_oracle_settings", return_value=SETTINGS.copy()), \
                patch.object(collector, "connect_oracle", return_value=oracle), \
                patch.object(collector, "poll_vsql", side_effect=[[self.query()], [], [self.query()]]), \
                patch.object(collector, "backfill_schema_names"), \
                patch.object(collector, "upsert_query", return_value=1), \
                patch.object(collector, "get_execution_plan", return_value=PLAN) as plans, \
                patch.object(collector, "fetch_bind_values", return_value=[]) as binds, \
                patch.object(collector, "save_plan"), \
                patch("db.store.get_conn", return_value=self.database()), \
                patch("db.store.get_setting", side_effect=lambda key, default="": default), \
                patch("db.store.report_service"), \
                patch.object(collector.console, "print"), patch.object(collector.console, "rule"), \
                patch.object(collector.time, "monotonic", return_value=0), \
                patch.object(collector.time, "sleep", side_effect=[None, None, KeyboardInterrupt]):
            collector._run_collector()
        plans.assert_called_once()
        binds.assert_called_once()
        oracle.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
