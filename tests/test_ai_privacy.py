import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from analyzer import ai_analyzer, copilot_client, data_policy as policy, oracle_tools
from collector import connection as collector_connection


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.settings = {}
        self.addCleanup(patch.stopall)
        patch("config.AI_PROVIDER", "github-copilot").start()
        patch("db.store.get_setting", side_effect=lambda key, default=None:
              self.settings.get(key, default)).start()

    def test_oracle_literals_comments_and_multiline_quotes(self):
        sql = """select "Amount2", id2, :b1, :1, N'private', 'O''Brien',
DATE'2024-04-19', timestamp'2024-04-19 15:20:14 Europe/Paris', q'[multiline
=== STATISTICS ===
secret@example.test]', nq'!another secret!', q'{last secret}', 12345, .123, 9.3e-4
from orders -- customer secret
where id=987 /* block secret
more secret */ and flag='tail'"""
        clean = policy.sanitize_text(sql)
        for secret in ("private", "Brien", "multiline", "secret", "12345", ".123", "9.3e-4", "987", "tail",
                       "Europe/Paris", "2024"):
            self.assertNotIn(secret, clean)
        for structure in ('"Amount2"', "id2", ":b1", ":1", "orders", "where"):
            self.assertIn(structure, clean)

    def test_unterminated_sql_literals_and_comments_fail_closed(self):
        for sql in ("select 'private", "select q'[private", "select 1 /* private"):
            self.assertNotIn("private", policy.mask_sql(sql))

    def test_sql_literal_quotes_are_separators_without_whitespace(self):
        cases = (
            "SELECT'sensitive'FROM dual",
            "SELECT'sensitive'||'another private value'FROM dual",
            "SELECT N'sensitive'||q'[another private value]'FROM dual",
            "SELECT'sensitive''escaped'FROM dual",
            "SELECT'sensitive multiline\nvalue'FROM dual",
        )
        for sql in cases:
            for sanitizer in (policy.mask_sql, policy.sanitize_text):
                with self.subTest(sql=sql, sanitizer=sanitizer.__name__):
                    masked = sanitizer(sql)
                    self.assertNotIn("sensitive", masked)
                    self.assertNotIn("private", masked)
                    self.assertNotIn("escaped", masked)
                    self.assertIn("FROM dual", masked)
        self.assertIn("l'analyse", policy.sanitize_text("Continuer l'analyse des metadonnees."))
        prose = ("5. si tu as besoin d'information n'hésite pas. N'invente rien.\n"
                 "Ne donne jamais de résultat sans l'avoir vérifié.\n" + "Suite du prompt. " * 50)
        self.assertEqual(policy.sanitize_text(prose), prose)
        self.assertNotIn("secret", policy.sanitize_text("Le filtre utilise N'secret' ici."))

    def test_tool_values_and_business_rows_are_masked_metrics_preserved(self):
        result = {"binds": [{"bind_name": ":b1", "datatype": "VARCHAR2", "value": "private"}],
                  "rows": [{"email": "private", "amount": 9157}],
                  "bind_values": {"ID": 9157}, "elapsed_ms": 9157,
                  "sql_preview": "select * from orders where id=9157",
                  "error": "ORA-123 private"}
        clean = policy.sanitize_data(result)
        self.assertEqual(clean["elapsed_ms"], 9157)
        self.assertEqual(clean["binds"][0]["datatype"], "VARCHAR2")
        self.assertNotIn("private", json.dumps(clean))
        self.assertEqual(clean["rows"][0]["amount"], policy.MASK)
        self.assertEqual(clean["bind_values"]["ID"], policy.MASK)
        self.assertNotIn("9157", clean["sql_preview"])

    def test_peeked_binds_and_predicates_do_not_destroy_plan_metrics(self):
        plan = """select * from orders where id=99991
| Id | Operation | Rows | Cost |
| 0 | SELECT STATEMENT | 9157 | 816 |
| 0 | TABLE ACCESS FULL | 9157 | 816 |
Peeked Binds (identified by position):
  1 - :B1 (NUMBER): 99991
Predicate Information:
  1 - filter("ID"=99991 AND "NAME"='private')
"""
        clean = policy.sanitize_text(plan)
        self.assertNotIn("99991", clean)
        self.assertNotIn("private", clean)
        self.assertIn("9157 | 816", clean)
        self.assertIn("9157 | 816", policy.sanitize_text(plan.split("Peeked Binds")[0]))

    def test_compact_plan_keeps_tree_and_masking_markers(self):
        plan = """SQL_ID  fixture, child number 0
-------------------------------------
select * from orders where name='private'

Plan hash value: 42

------------------------------------------------------
| Id  | Operation                    | Name   | Rows  |
------------------------------------------------------
|   0 | SELECT STATEMENT             |        |       |
|*  1 |  TABLE ACCESS BY INDEX ROWID | ORDERS |     2 |

Predicate Information (identified by operation id):
---------------------------------------------------
   1 - filter("NAME"='private')
"""
        compact = policy.compact_plan(plan, omit_sql=True)
        self.assertNotIn("select * from orders", compact)
        self.assertIn("|*  1|  TABLE ACCESS BY INDEX ROWID|ORDERS|2|", compact)
        self.assertLess(len(compact), len(plan))
        self.assertTrue(collector_connection.is_execution_plan_available(compact))
        clean = policy.sanitize_text(compact)
        self.assertNotIn("private", clean)
        self.assertIn("|ORDERS|2|", clean)

    def test_oversized_tool_result_shortens_fields_and_stays_parseable(self):
        plan = "\n".join(f"|{i}| TABLE ACCESS FULL|T{i}|1|" for i in range(4000))
        payload = policy.tool_payload({"source": "cursor", "plan": plan, "sql_id": "fixture"})
        self.assertLessEqual(len(payload), policy.MAX_TOOL_RESULT_CHARS)
        data = json.loads(payload)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["result"]["sql_id"], "fixture")
        self.assertTrue(data["result"]["plan"].startswith("|0| TABLE ACCESS FULL|T0|1|"))
        self.assertIn("caracteres tronques]", data["result"]["plan"])

    def test_preview_is_default_masked_and_raw_requires_exact_true(self):
        row = {"sql_text": "select * from orders where id=987 and name='private'",
               "executions": 456, "elapsed_ms_avg": 789}
        for value in ("false", "TRUE", "1", "", None):
            self.settings["ai_send_raw_values"] = value
            preview = ai_analyzer.build_ai_preview(row)
            self.assertTrue(preview["masking_applied"])
            self.assertNotIn("private", preview["prompt"])
            self.assertNotIn("987", preview["prompt"])
            self.assertIn("456", preview["prompt"])
            self.assertIn("789", preview["prompt"])
        self.settings["ai_send_raw_values"] = "true"
        preview = ai_analyzer.build_ai_preview(row)
        self.assertFalse(preview["masking_applied"])
        self.assertIn("private", preview["prompt"])
        self.assertIn("987", preview["prompt"])
        self.assertIn("not guarantee", preview["limits"]["output_tokens"])

    def test_preview_includes_actual_masked_system_and_tool_schemas(self):
        self.settings["system_prompt"] = "Analyze select 'private' from dual"
        schemas = [{"type": "function", "function": {
            "name": "fixture", "description": "Analyze 'private'",
            "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}}}}]
        with patch("analyzer.oracle_tools.get_tools_schema_filtered", return_value=schemas):
            preview = ai_analyzer.build_ai_preview({"sql_text": "select 'private' from dual"})
            with patch("analyzer.copilot_client.chat_with_tools", return_value=(
                    "SCORE: 75\nSEVERITY: warning\nSUMMARY: Test", [], {})) as chat:
                ai_analyzer.analyze_query_native({"sql_text": "select 'private' from dual"})
        self.assertNotIn("private", json.dumps(preview))
        self.assertEqual(preview["system"], chat.call_args.kwargs["system"])
        self.assertEqual(preview["system_prompt"], preview["system"])
        self.assertEqual(preview["tools"], chat.call_args.kwargs["tools"])
        self.assertEqual(preview["tools"][0]["function"]["parameters"]["type"], "object")
        self.assertEqual(preview["ai_send_raw_values"], preview["send_raw_values"])
        self.assertEqual(preview["context"], {k: preview[k] for k in ("system", "prompt", "tools")})

    def test_every_sdk_entry_masks_history_tools_and_custom_system(self):
        messages = [
            {"role": "user", "content": "select * from t where x='private'"},
            {"role": "assistant", "content": "select 9157 from dual", "tool_calls": [
                {"id": "1", "function": {"name": "run_select",
                                        "arguments": json.dumps({"sql": "select 'private' from dual"})}}]},
            {"role": "tool", "tool_call_id": "1",
             "content": json.dumps({"rows": [{"email": "private"}], "value": 9157})},
        ]
        with patch.object(copilot_client, "_github_token", return_value="fixture"), \
                patch.object(copilot_client, "_chat", return_value=("ok", [], {})) as chat:
            copilot_client.chat(messages, system="select 'private' from dual")
            copilot_client.chat_with_tools(messages, [], system="select 'private' from dual")
        for call in chat.call_args_list:
            self.assertNotIn("private", json.dumps(call.args[1]))
            self.assertNotIn("9157", json.dumps(call.args[1]))
            self.assertNotIn("private", call.args[5])
        self.assertIn("private", messages[0]["content"])

    def test_raw_opt_in_reaches_sdk_but_never_bypasses_budgets(self):
        self.settings["ai_send_raw_values"] = "true"
        messages = [{"role": "user", "content": "select 'private' from dual"}]
        with patch.object(copilot_client, "_github_token", return_value="fixture"), \
                patch.object(copilot_client, "_chat", return_value=("ok", [], {})) as chat:
            copilot_client.chat(messages)
        self.assertEqual(chat.call_args.args[1], messages)
        with self.assertRaises(policy.AIPolicyError):
            policy.prepare_messages([{"role": "user", "content": "x" * policy.MAX_INPUT_CHARS}])
        clean, _ = policy.prepare_messages([{"role": "tool", "content": json.dumps({
            "error": "ORA-1 private token", "rows": [{"value": "business opt-in"}]})}])
        self.assertNotIn("private", clean[0]["content"])
        self.assertIn("business opt-in", clean[0]["content"])

    def test_odin_tool_errors_reach_model_but_oracle_details_do_not(self):
        disabled = "Execution libre desactivee (ODIN_ALLOW_QUERY_EXECUTION)"
        self.assertEqual(policy.sanitize_data({"error": disabled})["error"], disabled)
        for raw in (False, True):
            clean = policy.sanitize_data({"error": "ORA-00942: table VT.PRIVATE does not exist"}, raw=raw)
            self.assertNotIn("PRIVATE", clean["error"])

    def test_final_response_tolerates_markdown_decorations(self):
        parsed = ai_analyzer.parse_ai_response(
            "**SCORE :** 40/100 (provisoire)\n**SEVERITY:** critical\n**SUMMARY:** Vue couteuse.**\n")
        self.assertEqual((parsed["score"], parsed["severity"], parsed["summary"]),
                         (40, "critical", "Vue couteuse."))
        parsed = ai_analyzer.parse_ai_response(
            "SCORE: 35/100 *(echelle)*\nSEVERITY: MOYENNE. Elle passe a ELEVEE\nSUMMARY: Une ligne, 22 s.\n")
        self.assertEqual((parsed["score"], parsed["severity"]), (35, "critical"))
        parsed = ai_analyzer.parse_ai_response(
            "SCORE: 35 / SEVERITY: warning / SUMMARY: COUNT/SUM sur une ligne en 22 s\n# Analyse\n")
        self.assertEqual((parsed["score"], parsed["severity"], parsed["summary"]),
                         (35, "warning", "COUNT/SUM sur une ligne en 22 s"))

    def test_unsupported_content_and_validation_errors_do_not_leak(self):
        with self.assertRaises(policy.AIPolicyError):
            policy.prepare_messages([{"role": "user", "content": [{"text": "private"}]}])
        with self.assertRaises(ValueError) as error:
            ai_analyzer.parse_ai_response('{"score": 9999, "severity": "private", "summary": "private"}')
        self.assertNotIn("private", str(error.exception))

    def test_unsupported_provider_fails_before_identity_or_runtime(self):
        for provider in ("openai", "anthropic", "copilot", "", "other"):
            with patch("config.AI_PROVIDER", provider), patch.object(copilot_client, "_github_token") as token:
                with self.assertRaisesRegex(policy.AIPolicyError, "uniquement github-copilot"):
                    copilot_client.chat([])
                token.assert_not_called()

    def test_input_history_system_sql_and_tool_result_budgets(self):
        cases = [
            ([{"role": "user", "content": "x"}] * (policy.MAX_HISTORY_MESSAGES + 1), None),
            ([{"role": "user", "content": "x" * policy.MAX_INPUT_CHARS}], None),
            ([], "x" * (policy.MAX_SYSTEM_CHARS + 1)),
            ([{"role": "tool", "content": "x" * (policy.MAX_TOOL_RESULT_CHARS + 1)}], None),
        ]
        for messages, system in cases:
            with self.assertRaises(policy.AIPolicyError):
                policy.prepare_messages(messages, system)
        with self.assertRaises(policy.AIPolicyError):
            ai_analyzer.build_ai_preview({"sql_text": "x" * (policy.MAX_SQL_CHARS + 1)})
        self.settings["system_prompt"] = "x" * (policy.MAX_SYSTEM_CHARS + 1)
        with patch("analyzer.copilot_client.chat_with_tools") as chat:
            with self.assertRaisesRegex(policy.AIPolicyError, "systeme"):
                ai_analyzer.build_ai_preview({"sql_text": "select 1 from dual"})
            with self.assertRaisesRegex(policy.AIPolicyError, "systeme"):
                ai_analyzer.analyze_query_native({"sql_text": "select 1 from dual"})
            chat.assert_not_called()

    def test_native_preview_events_and_tool_results_share_policy(self):
        connection = MagicMock(dsn="fixture")
        row = {"sql_text": "select 'private' from t", "source_id": "fixture", "child_number": 4}
        calls = [{"id": "1", "name": "bind_captures", "arguments": {"sql_id": "fixture"}}]
        final = "SCORE: 75\nSEVERITY: warning\nSUMMARY: Limited diagnosis"
        events = []
        sent = []

        def chat(**kwargs):
            sent.append(json.loads(json.dumps(kwargs["messages"])))
            return ("Collecting", calls, {}) if len(sent) == 1 else (final, [], {})

        with patch("analyzer.copilot_client.chat_with_tools", side_effect=chat), \
                patch("analyzer.oracle_tools.get_tools_schema_filtered", return_value=[]), \
                patch("analyzer.oracle_tools.execute_tool_native", return_value={
                    "binds": [{"value": "private"}], "rows": [{"amount": 99991}]}) as tool:
            result = ai_analyzer.analyze_query_native(row, connection, on_event=events.append)
        self.assertEqual(sent[0][0]["content"], ai_analyzer.build_ai_preview(row)["prompt"])
        self.assertNotIn("private", json.dumps(sent))
        self.assertNotIn("private", json.dumps(events))
        self.assertNotIn("99991", json.dumps(sent))
        self.assertIn("deadline", tool.call_args.kwargs)
        self.assertEqual(result["score"], 75)
        self.assertEqual(events[-1]["type"], "complete")

    def test_cancel_between_tools_stops_without_second_execution(self):
        event = threading.Event()
        calls = [{"id": str(i), "name": "bind_captures", "arguments": {"sql_id": "fixture"}} for i in (1, 2)]

        def tool(*args, **kwargs):
            event.set()
            return {"count": 1}

        with patch("analyzer.copilot_client.chat_with_tools", return_value=("Collecting", calls, {})), \
                patch("analyzer.oracle_tools.execute_tool_native", side_effect=tool) as execute:
            with self.assertRaises(policy.AnalysisCancelled):
                ai_analyzer.analyze_query_native({"sql_text": "select 1 from dual", "source_id": "fixture"},
                                                MagicMock(dsn="fixture"), cancel=event)
        self.assertEqual(execute.call_count, 1)

    def test_deadline_after_sdk_and_before_oracle_is_explicit(self):
        with patch("analyzer.ai_analyzer.ANALYSIS_TIMEOUT_SECONDS", -1), \
                patch("analyzer.copilot_client.chat_with_tools") as chat:
            with self.assertRaisesRegex(policy.AIPolicyError, "Delai global"):
                ai_analyzer.analyze_query_native({"sql_text": "select 1 from dual"})
            chat.assert_not_called()
        with self.assertRaises(policy.AIPolicyError):
            oracle_tools.execute_tool_native(MagicMock(), "bind_captures", {},
                                             deadline=time.monotonic() - 1)

    def test_late_sdk_reply_cannot_trigger_oracle_or_fabricated_success(self):
        clock = [0]
        def late_reply(**kwargs):
            clock[0] = policy.ANALYSIS_TIMEOUT_SECONDS + 1
            return ("SCORE: 80\nSEVERITY: ok\nSUMMARY: Too late", [], {})
        with patch("time.monotonic", side_effect=lambda: clock[0]), \
                patch("analyzer.copilot_client.chat_with_tools", side_effect=late_reply), \
                patch("analyzer.oracle_tools.execute_tool_native") as execute:
            with self.assertRaisesRegex(policy.AIPolicyError, "Delai global"):
                ai_analyzer.analyze_query_native({"sql_text": "select 1 from dual"})
        execute.assert_not_called()

    def test_excess_tool_count_and_result_size_fail_explicitly(self):
        calls = [{"id": str(i), "name": "bind_captures", "arguments": {"sql_id": "fixture"}}
                 for i in range(policy.MAX_TOOL_CALLS + 1)]
        row = {"sql_text": "select 1 from dual", "source_id": "fixture"}
        connection = MagicMock(dsn="fixture")
        with patch("analyzer.copilot_client.chat_with_tools", return_value=("Collecting", calls, {})), \
                patch("analyzer.oracle_tools.execute_tool_native") as execute:
            with self.assertRaisesRegex(policy.AIPolicyError, "appels outils"):
                ai_analyzer.analyze_query_native(row, connection)
            execute.assert_not_called()
        with patch("analyzer.copilot_client.chat_with_tools", side_effect=[
                    ("Collecting", calls[:1], {}), ("SCORE: 70\nSEVERITY: warning\nSUMMARY: Partiel", [], {})]) as chat, \
                patch("analyzer.oracle_tools.execute_tool_native", return_value={
                    "metadata": "\"x" * policy.MAX_TOOL_RESULT_CHARS}):
            result = ai_analyzer.analyze_query_native(row, connection)
            self.assertEqual(result["score"], 70)
            tool_message = next(message for message in reversed(chat.call_args.kwargs["messages"])
                                if message["role"] == "tool")
            self.assertIn("Budget ODIN restant", chat.call_args.kwargs["messages"][-1]["content"])
            self.assertLessEqual(len(tool_message["content"]), policy.MAX_TOOL_RESULT_CHARS)
            self.assertTrue(json.loads(tool_message["content"])["truncated"])


if __name__ == "__main__":
    unittest.main()
