import unittest
from unittest.mock import MagicMock, patch

from analyzer.oracle_tools import run_select
from collector.connection import connect_oracle


class OracleLimitsTests(unittest.TestCase):
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