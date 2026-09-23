import unittest
from unittest.mock import MagicMock, call, patch

from analyzer import ai_analyzer


class NativeModeTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch("config.AI_PROVIDER", "github-copilot").start()

    def test_legacy_mode_settings_cannot_select_legacy_analysis(self):
        row = {"sql_id": "fixture"}
        connection = object()
        for mode in ("classic", "agentic", "native", None):
            with self.subTest(mode=mode), \
                    patch("db.store.get_setting", return_value=mode), \
                    patch.object(ai_analyzer, "analyze_query_native", return_value={"score": 75}) as native:
                self.assertEqual(ai_analyzer.analyze_query(row, connection), {"score": 75})
                native.assert_called_once_with(row, oracle_conn=connection)

    def test_native_uses_saved_model_length_prompt_and_plan_limit(self):
        settings = {"ai_model": "saved-model", "ai_max_tokens": "8000", "system_prompt": "Custom native",
                    "plan_truncate": "100"}
        with patch("db.store.get_setting", side_effect=lambda key, default=None: settings.get(key, default)), \
                patch("analyzer.oracle_tools.get_tools_schema_filtered", return_value=[]), \
                patch("analyzer.copilot_client.chat_with_tools", return_value=(
                    "SCORE: 75\nSEVERITY: warning\nSUMMARY: Test", [], {})) as chat:
            result = ai_analyzer.analyze_query({
                "sql_text": "select 1 from dual",
                "plan_text": "| Id | Operation |\n| 0 | SELECT STATEMENT |\n" + "X" * 200})
        self.assertEqual(result["model"], "saved-model")
        self.assertEqual(chat.call_args.kwargs["model"], "saved-model")
        self.assertEqual(chat.call_args.kwargs["max_tokens"], 8000)
        self.assertEqual(chat.call_args.kwargs["system"], "Custom native")
        self.assertEqual(chat.call_args.kwargs["tools"], [])
        self.assertNotIn("X" * 101, chat.call_args.kwargs["messages"][0]["content"])
        self.assertIn("[plan tronqué...]", chat.call_args.kwargs["messages"][0]["content"])

    def test_default_prompt_allows_conclusion_without_calling_available_tools(self):
        from analyzer.oracle_tools import SYSTEM_NATIVE_ANALYZE, SYSTEM_NATIVE_CHAT
        tool = {"type": "function", "function": {
            "name": "describe_table", "description": "Table metadata",
            "parameters": {"type": "object", "properties": {
                "table_name": {"type": "string"}}, "required": ["table_name"]}}}
        with patch("db.store.get_setting", side_effect=lambda key, default="": default), \
                patch("analyzer.oracle_tools.get_tools_schema_filtered", return_value=[tool]), \
                patch("analyzer.oracle_tools.execute_tool_native") as execute, \
                patch("analyzer.copilot_client.chat_with_tools", return_value=(
                    "SCORE: 80\nSEVERITY: ok\nSUMMARY: Diagnostic limité au contexte fourni.", [], {})) as chat:
            result = ai_analyzer.analyze_query({"sql_text": "select 1 from dual"})
        self.assertEqual(result["score"], 80)
        chat.assert_called_once()
        self.assertEqual(chat.call_args.kwargs["tool_choice"], "auto")
        self.assertEqual(chat.call_args.kwargs["tools"], [tool])
        self.assertEqual(chat.call_args.kwargs["system"], SYSTEM_NATIVE_ANALYZE)
        self.assertIn("Aucun appel d’outil n’est obligatoire", chat.call_args.kwargs["system"])
        self.assertIn("ok pour 80-100, warning pour 50-79, critical pour 0-49",
                      chat.call_args.kwargs["system"])
        self.assertIn("SCORE: <entier 0-100>", SYSTEM_NATIVE_ANALYZE)
        self.assertNotIn("WORKFLOW OBLIGATOIRE EN DEUX TEMPS", SYSTEM_NATIVE_ANALYZE)
        self.assertNotIn("workflow en deux temps", SYSTEM_NATIVE_CHAT)
        execute.assert_not_called()

    def test_default_prompts_are_preserved_by_outbound_policy(self):
        from analyzer.data_policy import prepare_request
        from analyzer.oracle_tools import SYSTEM_NATIVE_ANALYZE, SYSTEM_NATIVE_CHAT
        for prompt in (SYSTEM_NATIVE_ANALYZE, SYSTEM_NATIVE_CHAT):
            with self.subTest(mode="analysis" if prompt == SYSTEM_NATIVE_ANALYZE else "chat"):
                _, system, _ = prepare_request([], system=prompt, raw=False)
                self.assertEqual(system, prompt)

    def test_automatic_refetches_plan_and_uses_one_connection_snapshot(self):
        self._automatic_refetch_case(True)

    def test_automatic_stale_archive_does_not_report_current_analysis_success(self):
        self._automatic_refetch_case(False)

    def test_automatic_old_provider_error_is_version_guarded_and_ignored(self):
        self._automatic_refetch_case(False, fail=True)

    def test_automatic_current_provider_error_is_version_guarded(self):
        self._automatic_refetch_case(True, fail=True)

    def _automatic_refetch_case(self, current_result, fail=False):
        connection = MagicMock(dsn="fixture")
        queue_conn = MagicMock()
        queue_conn.execute.return_value.fetchone.return_value = None
        current = {"id": 7, "sql_text": "select 2 from dual", "source_id": "fixture", "plan_id": 22}
        analysis = {"score": 80, "severity": "ok", "summary": "Result"}
        with patch("db.store.get_setting", side_effect=lambda key, default="": "auto" if key == "analyzer_mode" else default), \
                patch("db.store.report_service") as report, patch("db.store.get_conn", return_value=queue_conn), \
                patch("db.store.analyzing_queue_add", return_value=True), \
                patch("db.store.analyzing_queue_remove") as remove, \
                patch("db.store.get_query_detail", return_value={"query": current, "plan": {"plan_text": "current plan"}}), \
                patch("collector.connection.get_oracle_settings", return_value={
                    "oracle_dsn": "fixture", "oracle_user": "fixture", "oracle_password": "fixture"}) as settings, \
                patch.object(ai_analyzer, "get_unanalyzed", return_value=[
                    {"id": 7, "sql_text": "old sql", "plan_id": 1}]), \
                patch.object(ai_analyzer, "connect_oracle", return_value=connection) as connect, \
                patch.object(ai_analyzer, "analyze_query", return_value=analysis,
                             side_effect=RuntimeError("private provider failure") if fail else None) as analyze, \
                patch.object(ai_analyzer, "save_analysis", return_value=current_result) as save, \
                patch("db.store.save_analysis_error", return_value=current_result) as save_error, \
                patch("rich.console.Console"), patch("analyzer.ai_analyzer.time.sleep"):
            ai_analyzer._run_analyzer(once=True)
        settings.assert_called_once()
        self.assertEqual(connect.call_args.kwargs["dsn"], "fixture")
        analyzed_row = analyze.call_args.args[0]
        self.assertEqual(analyzed_row["sql_text"], current["sql_text"])
        self.assertEqual(analyzed_row["plan_text"], "current plan")
        if fail:
            save.assert_not_called()
            save_error.assert_called_once_with(
                7, "Analyse echouee. Consultez les journaux puis relancez.", expected_plan_id=22)
            if current_result:
                self.assertIn(call("analyzer", "error"), report.call_args_list)
            else:
                self.assertNotIn(call("analyzer", "error"), report.call_args_list)
        else:
            save.assert_called_once_with(7, analysis, expected_plan_id=22)
            save_error.assert_not_called()
        if current_result and not fail:
            self.assertIn(call("analyzer", "waiting", success=True), report.call_args_list)
        else:
            self.assertNotIn(call("analyzer", "waiting", success=True), report.call_args_list)
        remove.assert_called_once_with(7)
        connection.close.assert_called_once()

    def test_source_mismatch_prevents_native_model_and_tools(self):
        with patch("analyzer.copilot_client.chat_with_tools") as chat:
            with self.assertRaisesRegex(ValueError, "Source"):
                ai_analyzer.analyze_query_native(
                    {"source_id": "old", "sql_text": "select 1 from dual"}, MagicMock(dsn="new"))
        chat.assert_not_called()