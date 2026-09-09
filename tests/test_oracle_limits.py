import unittest
from unittest.mock import MagicMock, patch

from analyzer.oracle_tools import run_select, get_active_tools, get_tools_schema_filtered, execute_tool_native, TOOLS_PUBLIC
from collector.connection import connect_oracle


class OracleLimitsTests(unittest.TestCase):
    def test_tool_selection_preserves_defaults_and_explicit_none(self):
        for selected, expected in (("", set(TOOLS_PUBLIC) - {"gather_table_stats"}),
                                   ("none", set()), ("describe_table", {"describe_table"})):
            for gather in (False, True):
                with self.subTest(selected=selected, gather=gather), patch(
                    "db.store.get_setting", side_effect=lambda key, default="": {
                        "tools_enabled": selected, "gather_stats_enabled": str(gather).lower()
                    }.get(key, default)
                ):
                    active = expected | ({"gather_table_stats"} if gather else set())
                    self.assertEqual(get_active_tools(), active)
                    self.assertEqual({tool["function"]["name"] for tool in get_tools_schema_filtered()}, active)

    def test_explicit_none_blocks_execution(self):
        with patch("db.store.get_setting", side_effect=lambda key, default="":
                   "none" if key == "tools_enabled" else "false"), \
                patch.dict("analyzer.oracle_tools.TOOLS", {"describe_table": MagicMock()}) as tools:
            result = execute_tool_native(None, "describe_table", {"table_name": "ORDERS"})
            self.assertIn("error", result)
            tools["describe_table"].assert_not_called()

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