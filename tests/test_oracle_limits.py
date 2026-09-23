import unittest
import time
from unittest.mock import MagicMock, patch

from analyzer.oracle_tools import run_select, get_active_tools, get_tools_schema_filtered, execute_tool_native, TOOLS_PUBLIC
from collector.connection import connect_oracle
from analyzer import oracle_tools
from analyzer.data_policy import AIPolicyError


class OracleLimitsTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("db.store.get_setting", side_effect=lambda key, default="": default).start()

    def test_tool_selection_preserves_defaults_and_explicit_none(self):
        for selected, expected in (("", set(TOOLS_PUBLIC) - {"gather_table_stats"}),
                                   ("none", set()), ("describe_table", {"describe_table"})):
            for gather in (False, True):
                with self.subTest(selected=selected, gather=gather), patch(
                    "db.store.get_setting", side_effect=lambda key, default="": {
                        "tools_enabled": selected, "gather_stats_enabled": str(gather).lower()
                    }.get(key, default)
                ), patch.dict("os.environ", {"ODIN_ALLOW_QUERY_EXECUTION": "true"}):
                    active = expected | ({"gather_table_stats"} if gather else set())
                    self.assertEqual(get_active_tools(), active)
                    self.assertEqual({tool["function"]["name"] for tool in get_tools_schema_filtered()}, active)

    def test_free_select_is_not_offered_when_execution_is_disabled(self):
        with patch.dict("os.environ", {"ODIN_ALLOW_QUERY_EXECUTION": "false"}):
            self.assertNotIn("run_select", get_active_tools())
            self.assertNotIn("run_select", {tool["function"]["name"] for tool in get_tools_schema_filtered()})

    def test_explicit_none_blocks_execution(self):
        with patch("db.store.get_setting", side_effect=lambda key, default="":
                   "none" if key == "tools_enabled" else "false"), \
                patch.dict("analyzer.oracle_tools.TOOLS", {"describe_table": MagicMock()}) as tools:
            result = execute_tool_native(None, "describe_table", {"table_name": "ORDERS"})
            self.assertIn("error", result)
            tools["describe_table"].assert_not_called()

    def test_monitor_and_awr_plan_respect_management_pack_access(self):
        def fake_query(conn, sql, params=None):
            if "control_management_pack_access" in sql:
                return [{"value": "NONE"}]
            return []
        with patch.object(oracle_tools, "_query", side_effect=fake_query):
            self.assertIn("Tuning Pack", oracle_tools.sql_monitor(None, "fixture")["error"])
            self.assertIn("Diagnostics Pack",
                          oracle_tools.cursor_plan(None, "fixture", plan_hash_value=42)["error"])
        self.assertIn("error", oracle_tools.cursor_plan(None, "fixture"))
        self.assertIn("error", oracle_tools.cursor_plan(None, "fixture", child_number="1;drop"))
        self.assertTrue({"cursor_plan", "sql_monitor"} <= set(TOOLS_PUBLIC))
        names = {entry["function"]["name"] for entry in oracle_tools.TOOLS_SCHEMA}
        self.assertTrue({"cursor_plan", "sql_monitor"} <= names)

    def test_free_select_requires_opt_in(self):
        with patch.dict('os.environ', {"ODIN_ALLOW_QUERY_EXECUTION": "false"}):
            self.assertIn("error", run_select(MagicMock(), "select * from orders"))

    def test_existing_rownum_cannot_bypass_limit(self):
        with patch.dict('os.environ', {"ODIN_ALLOW_QUERY_EXECUTION": "true"}), patch('analyzer.oracle_tools._query', return_value=[]) as execute:
            result = run_select(MagicMock(), "select * from orders where rownum < 100000", "999")
            self.assertEqual(result["limited_to"], 50)
            self.assertTrue(execute.call_args.args[1].endswith("WHERE ROWNUM <= 50"))

    def test_connection_is_bounded_and_identified(self):
        with patch('collector.connection.oracledb.connect') as connect:
            connection = connect_oracle(user="test", password="test", dsn="test")
            self.assertEqual(connect.call_args.kwargs["tcp_connect_timeout"], 10)
            self.assertGreater(connection.call_timeout, 0)
            self.assertEqual(connection.module, "oracleiq")

    def test_awr_totals_are_computed_before_detail_limit_with_rac_key(self):
        rows = [{"executions": 2, "total_elapsed_sec": 1, "plan_hash_value": 11,
                 "period_executions": 600, "period_elapsed_us": 9_000_000,
                 "period_sqlstat_rows": 90, "period_plan_count": 3,
                 "period_buffer_gets": 7000, "period_disk_reads": 42,
                 "coverage_start": "start", "coverage_end": "end"}]
        with patch.object(oracle_tools, "_query", return_value=rows) as query:
            result = oracle_tools.awr_sql_stats(None, "fixture")
        sql = query.call_args.args[1]
        self.assertIn("sn.instance_number = s.instance_number", sql)
        self.assertIn("sn.dbid = s.dbid", sql)
        self.assertIn("sn.snap_id = s.snap_id", sql)
        self.assertIn("SUM(s.executions_delta) OVER ()", sql)
        self.assertIn("FETCH FIRST 20 ROWS ONLY", sql)
        self.assertNotIn("s.executions_delta > 0", sql)
        summary = result["awr_summary"]
        self.assertEqual(summary["total_executions"], 600)
        self.assertEqual(summary["total_elapsed_sec"], 9)
        self.assertEqual(summary["avg_elapsed_ms"], 15)
        self.assertEqual(summary["sqlstat_rows_count"], 90)
        self.assertTrue(summary["details_truncated"])
        self.assertEqual(summary["details_returned"], 1)
        self.assertEqual(summary["distinct_plan_count"], 3)

    def test_awr_top_sql_uses_complete_rac_key_and_period_aggregation(self):
        with patch.object(oracle_tools, "_query", return_value=[{"sql_id": "fixture"}]) as query:
            result = oracle_tools.awr_top_sql(None, days="999", limit="999")
        self.assertIn("sn.instance_number = s.instance_number", query.call_args.args[1])
        self.assertIn("SUM(s.elapsed_time_delta)", query.call_args.args[1])
        self.assertEqual(query.call_args.args[2], {"d": 30, "lim": 50})
        self.assertEqual(result["details_limit"], 50)

    def test_bind_captures_filters_exact_child_in_both_queries(self):
        with patch.object(oracle_tools, "_query", side_effect=[[], [
                {"name": ":b1", "child_number": 7, "value": None}]]) as query:
            result = oracle_tools.bind_captures(None, "fixture", child_number=7)
        self.assertFalse(result["captured"])
        self.assertEqual(result["child_number"], 7)
        self.assertEqual(query.call_count, 2)
        for call_args in query.call_args_list:
            self.assertIn("(:child_no IS NULL OR b.child_number = :child_no)", call_args.args[1])
            self.assertEqual(call_args.args[2], {"sql_id": "fixture", "child_no": 7})

    def test_bind_captures_without_filter_never_merges_different_children(self):
        rows = [{"bind_name": ":b1", "position": 1, "child_number": child, "value": "fixture"}
                for child in (1, 2)]
        with patch.object(oracle_tools, "_query", return_value=rows):
            result = oracle_tools.bind_captures(None, "fixture")
        self.assertEqual(result["count"], 2)
        self.assertEqual({row["child_number"] for row in result["binds"]}, {1, 2})
        schema = next(entry["function"] for entry in oracle_tools.TOOLS_SCHEMA
                      if entry["function"]["name"] == "bind_captures")
        self.assertIn("child_number", schema["parameters"]["properties"])

    def explain_connection(self, plan=None, explain_error=False, owns_plan_table=1):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [("select * from orders where id=:b1", "APP"),
                                       ("ORIGINAL", "MONITOR", owns_plan_table)]
        cursor.fetchmany.return_value = [(line,) for line in
                                        (plan or ["| Id | Operation |", "| 0 | SELECT STATEMENT |"])]
        if explain_error:
            def execute(sql, **kwargs):
                if sql.startswith("EXPLAIN PLAN"):
                    raise RuntimeError("ORA-123 private bind")
            cursor.execute.side_effect = execute
        return connection, cursor

    def test_explain_uses_child_parsing_schema_owned_plan_table_and_restores(self):
        connection, cursor = self.explain_connection()
        result = oracle_tools.explain_plan(connection, "fixture", child_number=7)
        self.assertTrue(result["plan_valid"])
        self.assertEqual(result["plan_kind"], "estimated")
        self.assertEqual(result["child_number"], 7)
        calls = cursor.execute.call_args_list
        self.assertEqual(calls[0].kwargs, {"sid": "fixture", "child": 7})
        sqls = [call.args[0] for call in calls]
        self.assertIn('ALTER SESSION SET CURRENT_SCHEMA = "APP"', sqls)
        self.assertIn('INTO "MONITOR"."PLAN_TABLE"', sqls[3])
        self.assertEqual(sqls[-1], 'ALTER SESSION SET CURRENT_SCHEMA = "ORIGINAL"')
        connection.commit.assert_not_called()
        self.assertFalse(any(sql.startswith("select * from orders") for sql in sqls))

    def test_explain_falls_back_to_standard_plan_table_without_owned_table(self):
        connection, cursor = self.explain_connection(owns_plan_table=0)
        result = oracle_tools.explain_plan(connection, "fixture", child_number=7)
        self.assertTrue(result["plan_valid"])
        sqls = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn('INTO "SYS"."PLAN_TABLE$"', sqls[3])
        self.assertTrue(any(sql.startswith('DELETE FROM "SYS"."PLAN_TABLE$"') for sql in sqls))

    def test_explain_restores_on_error_and_never_returns_diagnostic_as_plan(self):
        for plan, fail in [(["Error: cannot fetch plan for statement"], False),
                           (["ORA-123 private"], False), (None, True)]:
            with self.subTest(plan=plan, fail=fail):
                connection, cursor = self.explain_connection(plan, fail)
                result = oracle_tools.explain_plan(connection, "fixture", 7)
                self.assertFalse(result["plan_valid"])
                self.assertIn("error", result)
                self.assertNotIn("private", str(result))
                self.assertEqual(cursor.execute.call_args.args[0],
                                 'ALTER SESSION SET CURRENT_SCHEMA = "ORIGINAL"')
                connection.commit.assert_not_called()

    def test_explain_missing_child_does_not_guess(self):
        connection = MagicMock()
        self.assertIn("error", oracle_tools.explain_plan(connection, "fixture"))
        connection.cursor.assert_not_called()

    def test_long_view_text_is_masked_then_paged_with_columns_once(self):
        text = "select a.x, 'private' as y\n" + "join t on t.id = a.id\n" * 2000
        responses = lambda offset: [
            [{"object_name": "V", "object_type": "VIEW", "owner": "APP"}],
            [{"text_length": len(text), "text": text}],
            *([[{"column_name": "X", "data_type": "NUMBER", "nullable": "Y"}]] if not offset else [])]
        with patch.object(oracle_tools, "raw_values_enabled", return_value=False):
            with patch.object(oracle_tools, "_query", side_effect=responses(0)):
                first = oracle_tools.describe_object(None, "V", "APP")["details"]["VIEW"]
            with patch.object(oracle_tools, "_query", side_effect=responses(1)):
                second = oracle_tools.describe_object(None, "V", "APP",
                                                      text_offset=first["next_text_offset"])["details"]["VIEW"]
        self.assertNotIn("private", first["view_text"])
        self.assertTrue(first["view_text"].endswith("\n"))
        self.assertEqual(first["columns"], [["X", "NUMBER", "Y"]])
        self.assertNotIn("columns", second)
        self.assertTrue(second["view_text"].startswith("join t"))
        self.assertIn("error", oracle_tools.describe_object(None, "V", text_offset="-1"))

    def test_shared_parsing_schema_restores_after_body_exception(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = ("ORIGINAL",)
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with oracle_tools.oracle_parsing_schema(connection, 'APP"QUOTED'):
                raise RuntimeError("fixture")
        sqls = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn('ALTER SESSION SET CURRENT_SCHEMA = "APP""QUOTED"', sqls)
        self.assertEqual(sqls[-1], 'ALTER SESSION SET CURRENT_SCHEMA = "ORIGINAL"')
        connection.close.assert_not_called()

    def test_shared_parsing_schema_closes_connection_if_restoration_fails(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = ("ORIGINAL",)
        def execute(sql):
            if sql.endswith('"ORIGINAL"'):
                raise RuntimeError("private Oracle diagnostic")
        cursor.execute.side_effect = execute
        with self.assertRaises(AIPolicyError) as raised:
            with oracle_tools.oracle_parsing_schema(connection, "APP"):
                pass
        self.assertNotIn("private", str(raised.exception))
        connection.close.assert_called_once()

    def test_each_oracle_roundtrip_receives_remaining_timeout_and_restores_original(self):
        connection = MagicMock(call_timeout=30000)
        cursor = connection.cursor.return_value
        cursor.description = [("COUNT",)]
        cursor.fetchmany.return_value = [(7,)]
        timeouts = []
        cursor.execute.side_effect = lambda *args: timeouts.append(connection.call_timeout)
        with patch.dict(oracle_tools.TOOLS, {"bind_captures": lambda conn: oracle_tools._query(conn, "SELECT 7 FROM dual")}):
            result = oracle_tools.execute_tool_native(connection, "bind_captures", {},
                                                       deadline=time.monotonic() + 2)
        self.assertEqual(result, [{"count": 7}])
        self.assertTrue(0 < timeouts[0] <= 2000)
        self.assertEqual(connection.call_timeout, 30000)

    def test_oracle_error_never_exposes_raw_exception(self):
        def failure(conn):
            raise RuntimeError("private literal token")
        with patch.dict(oracle_tools.TOOLS, {"bind_captures": failure}):
            result = oracle_tools.execute_tool_native(MagicMock(), "bind_captures", {},
                                                       deadline=time.monotonic() + 2)
        self.assertNotIn("private", str(result))