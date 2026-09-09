import unittest
from unittest.mock import patch

from analyzer import ai_analyzer


class NativeModeTests(unittest.TestCase):
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
            result = ai_analyzer.analyze_query({"sql_text": "select 1 from dual", "plan_text": "X" * 200})
        self.assertEqual(result["model"], "saved-model")
        self.assertEqual(chat.call_args.kwargs["model"], "saved-model")
        self.assertEqual(chat.call_args.kwargs["max_tokens"], 8000)
        self.assertEqual(chat.call_args.kwargs["system"], "Custom native")
        self.assertEqual(chat.call_args.kwargs["tools"], [])
        self.assertNotIn("X" * 101, chat.call_args.kwargs["messages"][0]["content"])