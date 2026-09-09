import unittest
from unittest.mock import MagicMock, patch

import oracleiq


class LauncherTests(unittest.TestCase):
    def processes(self):
        processes = []
        for index, name in enumerate(("collector", "analyzer", "webui")):
            process = MagicMock()
            process.name = name
            process.sentinel = index
            process.exitcode = 1
            process.is_alive.side_effect = [True, False]
            processes.append(process)
        return processes

    def test_any_child_failure_stops_siblings_and_returns_error(self):
        for failed_index in range(3):
            with self.subTest(failed_index=failed_index):
                processes = self.processes()
                processes[failed_index].is_alive.side_effect = [False, False]
                with patch("multiprocessing.Process", side_effect=processes), patch("builtins.print"):
                    with patch("multiprocessing.connection.wait", return_value=[failed_index]):
                        with self.assertRaises(SystemExit) as result:
                            oracleiq.cmd_all()
                self.assertEqual(result.exception.code, 1)
                for index, process in enumerate(processes):
                    process.start.assert_called_once()
                    if index != failed_index:
                        process.terminate.assert_called_once()
                    process.join.assert_any_call(timeout=5)

    def test_interrupt_stops_and_joins_children(self):
        processes = self.processes()
        with patch("multiprocessing.Process", side_effect=processes), patch("builtins.print"):
            with patch("multiprocessing.connection.wait", side_effect=KeyboardInterrupt):
                oracleiq.cmd_all()
        for process in processes:
            process.terminate.assert_called_once()
            process.join.assert_called_once_with(timeout=5)

    def test_start_failure_cleans_up_started_children(self):
        processes = self.processes()
        processes[1].start.side_effect = OSError("Cannot start")
        with patch("multiprocessing.Process", side_effect=processes), patch("builtins.print"):
            with self.assertRaises(OSError):
                oracleiq.cmd_all()
        processes[0].terminate.assert_called_once()
        processes[0].join.assert_called_once_with(timeout=5)
        processes[2].start.assert_not_called()