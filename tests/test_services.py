import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analyzer import ai_analyzer
from collector import oracle_collector
from db import store


class ServiceLifecycleTests(unittest.TestCase):
    def test_empty_successful_poll_records_success_before_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(store, "DB_PATH", Path(directory) / "services.db"):
                with patch.object(oracle_collector, "connect_oracle"), patch.object(oracle_collector, "poll_vsql", return_value=[]):
                    with patch.object(oracle_collector.time, "sleep", side_effect=KeyboardInterrupt):
                        oracle_collector.run_collector()
                status = store.get_service_health()["collector"]
                self.assertEqual(status["state"], "stopped")
                self.assertIsNotNone(status["last_success"])

    def test_collector_reports_start_failure(self):
        with patch.object(oracle_collector, "init_db"), patch("db.store.report_service") as report:
            with patch.object(oracle_collector, "_run_collector", side_effect=SystemExit(1)):
                with self.assertRaises(SystemExit):
                    oracle_collector.run_collector()
        self.assertEqual(report.call_args_list[-1].args, ("collector", "error"))

    def test_analyzer_reports_normal_stop(self):
        with patch("db.store.init_db"), patch("db.store.report_service") as report:
            with patch.object(ai_analyzer, "_run_analyzer") as run:
                ai_analyzer.run_analyzer(once=True, batch_size=2)
                run.assert_called_once_with(once=True, batch_size=2)
        self.assertEqual(report.call_args_list[-1].args, ("analyzer", "stopped"))