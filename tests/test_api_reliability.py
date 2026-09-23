import asyncio
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_database = tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".test-api-reliability-")
os.environ["ODIN_DB_PATH"] = str(Path(_database.name) / "bootstrap.db")

from db import store
store.DB_PATH = Path(os.environ["ODIN_DB_PATH"])
from fastapi import HTTPException
from fastapi.testclient import TestClient
from api import app as api
from api import security


RESULT = {"score": 90, "severity": "ok", "summary": "Synthetic analysis",
          "issues": [], "recommendations": [], "raw": "Synthetic analysis",
          "model": "fixture-model", "usage": {}, "trace": []}
PLAN = "| Id | Operation | Name |\n| 0 | SELECT STATEMENT | |"


class ApiReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".test-api-reliability-case-")
        self.addCleanup(self.database.cleanup)
        self.patch(store, "DB_PATH", Path(self.database.name) / "test.db")
        store.init_db()
        security._attempts.clear()
        with api._analysis_lock:
            api.analyzing_ids.clear()
            api._stream_queues.clear()
            api._analysis_results.clear()
            api._chat_ids.clear()
        self.client = TestClient(api.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set(security.COOKIE_NAME, security.make_token())
        self.patch(api, "get_oracle_settings", return_value={
            "oracle_user": "fixture", "oracle_password": "fixture", "oracle_dsn": "source-a"})
        self.patch(__import__("oracledb"), "connect", side_effect=AssertionError("Network forbidden"))
        self.patch(api._copilot_client, "chat_with_tools", side_effect=AssertionError("AI network forbidden"))
        self.qid = store.upsert_query({
            "source_id": "source-a", "sql_id": "fixture", "sql_hash": "fixture",
            "sql_text": "select 1 from dual", "schema_name": "APP", "child_number": 2,
        })

    def patch(self, target, name, *args, **kwargs):
        patcher = patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def connection(self, source="source-a"):
        connection = MagicMock()
        connection.dsn = source
        return connection

    def inline_jobs(self):
        return self.patch(api._analysis_executor, "submit",
                          side_effect=lambda function, *args: function(*args))

    def test_settings_are_atomic_and_privacy_defaults_to_masked(self):
        self.assertFalse(self.client.get("/api/settings").json()["ai_send_raw_values"])
        with patch.object(api, "set_settings") as save, patch.object(api, "set_setting") as single:
            result = self.client.post("/api/settings", json={
                "oracle_dsn": "new-source", "oracle_user": "new-user",
                "oracle_password": "synthetic-private", "ai_send_raw_values": True,
            })
            self.assertEqual(result.status_code, 200)
            self.assertNotIn("synthetic-private", result.text)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(save.call_args.args[0]["ai_send_raw_values"], "true")
            single.assert_not_called()
        self.assertEqual(self.client.post("/api/settings", json={"ai_send_raw_values": []}).status_code, 422)

    def test_settings_reject_limits_above_effective_ai_policy(self):
        for values in ({"ai_max_tokens": 32001}, {"plan_truncate": 40001},
                       {"system_prompt": "x" * 20001}):
            self.assertEqual(self.client.post("/api/settings", json=values).status_code, 422)
        response = self.client.post("/api/settings", json={
            "ai_max_tokens": 32000, "plan_truncate": 40000, "system_prompt": "x" * 20000})
        self.assertEqual(response.status_code, 200)

    def test_model_refresh_refuses_noncanonical_provider_before_network(self):
        with patch.object(api._copilot_client, "list_account_models") as models:
            for provider in ("copilot", "openai", "anthropic"):
                with patch("config.AI_PROVIDER", provider):
                    self.assertEqual(self.client.post("/api/models/refresh").status_code, 409)
            models.assert_not_called()

    def test_preview_is_admin_only_offline_and_uses_shared_builder(self):
        with patch("analyzer.ai_analyzer.build_ai_preview", return_value={"prompt": "masked", "limits": {}}) as preview, \
                patch.object(api, "connect_oracle") as connect:
            result = self.client.get(f"/api/queries/{self.qid}/ai-preview")
            self.assertEqual(result.json()["prompt"], "masked")
            self.assertEqual(result.json()["context"], {"prompt": "masked"})
            self.assertFalse(result.json()["ai_send_raw_values"])
            self.assertEqual(preview.call_args.args[0]["source_id"], "source-a")
            connect.assert_not_called()
            with patch.object(security, "PUBLIC_READ", True):
                for role in (None, "viewer"):
                    self.client.cookies.clear()
                    if role:
                        self.client.cookies.set(security.COOKIE_NAME, security.make_token(role))
                    self.assertEqual(self.client.get(f"/api/queries/{self.qid}/ai-preview").status_code, 403)
            self.assertEqual(preview.call_count, 1)

    def test_alerts_read_pagination_and_admin_acknowledgement(self):
        contract = {"items": [], "total": 0, "unacknowledged": 0}
        with patch("db.alerts.list_alerts", return_value=contract) as listing, \
                patch("db.alerts.acknowledge_alert", return_value=True) as acknowledge:
            self.client.cookies.clear()
            self.assertEqual(self.client.get("/api/alerts").status_code, 401)
            with patch.object(security, "PUBLIC_READ", True):
                response = self.client.get("/api/alerts?limit=10&offset=20&include_acknowledged=false")
                self.assertEqual(response.json(), contract)
                listing.assert_called_once_with(limit=10, offset=20, include_acknowledged=False)
                self.assertEqual(self.client.post("/api/alerts/1/acknowledge").status_code, 403)
            self.client.cookies.set(security.COOKIE_NAME, security.make_token("viewer"))
            self.assertEqual(self.client.get("/api/alerts").status_code, 200)
            self.assertEqual(self.client.post("/api/alerts/1/acknowledge").status_code, 403)
            self.client.cookies.set(security.COOKIE_NAME, security.make_token())
            self.assertEqual(self.client.post("/api/alerts/1/acknowledge").json(), {"ok": True})
            acknowledge.return_value = False
            self.assertEqual(self.client.post("/api/alerts/99/acknowledge").status_code, 404)
            for suffix in ("?limit=0", "?offset=-1", "?limit=101"):
                self.assertEqual(self.client.get("/api/alerts" + suffix).status_code, 422)

    def test_alert_lifecycle_runs_without_dashboard_and_stops(self):
        evaluated = threading.Event()
        with patch("db.alerts.evaluate_alerts", side_effect=lambda: evaluated.set()) as evaluate:
            with TestClient(api.app):
                self.assertTrue(evaluated.wait(2))
            self.assertEqual(evaluate.call_count, 1)

    def test_alert_evaluation_errors_are_logged_and_shutdown_completes(self):
        async def scenario():
            stop = asyncio.Event()
            def failure():
                stop.set()
                raise RuntimeError("synthetic")
            with patch("db.alerts.evaluate_alerts", side_effect=failure), \
                    patch.object(api._log, "exception") as logged:
                await api._alert_loop(stop)
                logged.assert_called_once()
        asyncio.run(scenario())

    def test_live_routes_reject_different_and_unknown_source_before_query_tools(self):
        for source in ("source-b", ""):
            connection = self.connection(source)
            with patch.object(api, "connect_oracle", return_value=connection), \
                    patch("collector.oracle_collector.get_execution_plan") as plan, \
                    patch("analyzer.oracle_tools.bind_captures") as binds, \
                    patch.object(api, "query_execution_enabled", return_value=True):
                for endpoint in (f"/api/refresh_plan/{self.qid}",
                                 f"/api/queries/{self.qid}/binds/refresh",
                                 f"/api/queries/{self.qid}/replay",
                                 f"/api/queries/{self.qid}/chat"):
                    result = self.client.post(endpoint, json={"message": "Explain"})
                    self.assertEqual(result.status_code, 409, endpoint)
                plan.assert_not_called()
                binds.assert_not_called()
                connection.cursor.assert_not_called()
                self.assertEqual(connection.close.call_count, 4)
                self.assertEqual(store.chat_get_messages(self.qid), [])

    def test_bulk_repair_checks_all_sources_before_any_repair(self):
        connection = store.get_conn()
        connection.execute("UPDATE queries SET sql_text=?", ("select " + "x" * 1000,))
        connection.commit()
        connection.close()
        oracle = self.connection("source-b")
        with patch.object(api, "connect_oracle", return_value=oracle), \
                patch("collector.oracle_collector.get_full_sql_text") as full:
            response = self.client.post("/api/repair_sql_texts")
            self.assertEqual(response.status_code, 409)
            full.assert_not_called()
            oracle.close.assert_called_once()

    def test_bind_refresh_targets_exact_child_cursor(self):
        oracle = self.connection()
        result = {"captured": True, "binds": [
            {"bind_name": ":id", "position": 1, "datatype": "NUMBER", "value": "123"}]}
        with patch.object(api, "connect_oracle", return_value=oracle), \
                patch("analyzer.oracle_tools.bind_captures", return_value=result) as captures:
            response = self.client.post(f"/api/queries/{self.qid}/binds/refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "oracle-live")
        captures.assert_called_once_with(oracle, "fixture", child_number=2)
        self.assertEqual(store.get_bind_values(self.qid)[0]["value_string"], "123")

    def test_replay_refuses_bound_queries_and_marks_execution_failure(self):
        connection = store.get_conn()
        connection.execute("UPDATE queries SET sql_text='select :id from dual' WHERE id=?", (self.qid,))
        connection.commit()
        connection.close()
        with patch.object(api, "query_execution_enabled", return_value=True), \
                patch.object(api, "connect_oracle") as connect:
            result = self.client.post(f"/api/queries/{self.qid}/replay").json()
            self.assertFalse(result["ok"])
            self.assertIn("bind", result["error"])
            connect.assert_not_called()
        connection = store.get_conn()
        connection.execute("UPDATE queries SET sql_text='select 1 from dual' WHERE id=?", (self.qid,))
        connection.commit()
        connection.close()
        oracle = self.connection()
        schema_cursor = MagicMock()
        schema_cursor.fetchone.return_value = ("APP",)
        failed_cursor = MagicMock()
        failed_cursor.execute.side_effect = RuntimeError("private-error")
        oracle.cursor.side_effect = [schema_cursor, failed_cursor, failed_cursor]
        with patch.object(api, "query_execution_enabled", return_value=True), \
                patch.object(api, "connect_oracle", return_value=oracle):
            response = self.client.post(f"/api/queries/{self.qid}/replay")
            self.assertFalse(response.json()["ok"])
            self.assertIn("exec_error", response.json())
            self.assertNotIn("private-error", response.text)
            oracle.close.assert_called_once()

    def test_bind_detection_ignores_oracle_literals_comments_and_identifiers(self):
        unbound = [
            "SELECT 'https://host:8080' FROM dual",
            "SELECT':id'FROM dual",
            "SELECT 'escaped'':id', N':id', n':1' FROM dual",
            "SELECT q'[quoted '' :id]' FROM dual",
            "SELECT q'{:id}', q'(:1)', q'<:id>', q'!:id!' FROM dual",
            "SELECT nq'[national :id]', NQ'!:1!' FROM dual",
            "SELECT 1 /* ignored :id */ FROM dual -- ignored :1\n",
            'SELECT "column:id", "escaped"":1" FROM "schema:id"."table:1"',
            "SELECT q'[multiline\n:id\n]' FROM dual",
        ]
        for sql in unbound:
            with self.subTest(sql=sql):
                self.assertFalse(api._has_replay_binds(sql))
        for sql in ("SELECT :id FROM dual", "SELECT :1 FROM dual",
                    'SELECT :"name" FROM dual', 'SELECT "column:id", :real FROM dual',
                    "SELECT q'[:not_a_bind]', :real FROM dual",
                    "SELECT 1 /* :ignored */ FROM dual WHERE 1=:1"):
            with self.subTest(sql=sql):
                self.assertTrue(api._has_replay_binds(sql))

    def test_replay_refuses_wrong_parsing_schema_before_application_sql(self):
        for current_schema in ("MONITOR", None):
            oracle = self.connection()
            oracle.cursor.return_value.fetchone.return_value = (current_schema,)
            with patch.object(api, "query_execution_enabled", return_value=True), \
                    patch.object(api, "connect_oracle", return_value=oracle):
                response = self.client.post(f"/api/queries/{self.qid}/replay")
                self.assertFalse(response.json()["ok"])
                self.assertIn("schema", response.json()["error"])
                oracle.cursor.return_value.execute.assert_called_once_with(
                    "SELECT SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM dual")
                oracle.close.assert_called_once()

    def test_replay_in_verified_parsing_schema_preserves_success(self):
        connection = store.get_conn()
        connection.execute("UPDATE queries SET sql_text=? WHERE id=?",
                           ("SELECT 'https://host:8080' FROM dual", self.qid))
        connection.commit()
        connection.close()
        oracle = self.connection()
        schema_cursor, explain_cursor, plan_cursor, execution_cursor = [MagicMock() for _ in range(4)]
        schema_cursor.fetchone.return_value = ("APP",)
        plan_cursor.fetchall.return_value = [(PLAN,)]
        execution_cursor.fetchall.return_value = [(1,)]
        execution_cursor.description = [("ONE",)]
        oracle.cursor.side_effect = [schema_cursor, explain_cursor, plan_cursor, execution_cursor]
        with patch.object(api, "query_execution_enabled", return_value=True), \
                patch.object(api, "connect_oracle", return_value=oracle):
            response = self.client.post(f"/api/queries/{self.qid}/replay")
        self.assertTrue(response.json()["ok"], response.text)
        self.assertEqual(response.json()["sample_rows"], [{"ONE": "1"}])
        self.assertEqual(response.json()["rows_returned"], 1)
        execution_cursor.execute.assert_called_once_with(
            "SELECT * FROM (SELECT 'https://host:8080' FROM dual) WHERE ROWNUM <= 5")
        oracle.close.assert_called_once()

    def test_analysis_source_failure_terminates_post_subscribers_and_releases(self):
        self.inline_jobs()
        with patch.object(api, "connect_oracle", return_value=self.connection("source-b")), \
                patch("analyzer.ai_analyzer.analyze_query") as analyze:
            self.assertEqual(self.client.post(f"/api/analyze/{self.qid}").status_code, 200)
            stream = self.client.get(f"/api/analyze/{self.qid}/stream")
            self.assertIn('"type": "error"', stream.text)
            analyze.assert_not_called()
        self.assertNotIn(self.qid, store.analyzing_queue_get())
        self.assertNotIn(self.qid, api.analyzing_ids)
        self.assertNotIn(self.qid, api._stream_queues)

    def test_sse_saves_versioned_result_and_uses_shared_orchestration(self):
        self.inline_jobs()
        with patch.object(api, "connect_oracle", return_value=self.connection()), \
                patch("collector.oracle_collector.get_execution_plan", return_value=PLAN), \
                patch("analyzer.ai_analyzer.analyze_query", return_value=RESULT) as analyze, \
                patch("db.store.save_analysis", wraps=store.save_analysis) as save:
            response = self.client.get(f"/api/analyze/{self.qid}/stream")
            self.assertIn('"type": "done"', response.text)
            self.assertEqual(analyze.call_count, 1)
            self.assertIsNotNone(save.call_args.kwargs["expected_plan_id"])
            self.assertEqual(store.get_query_detail(self.qid)["analysis"]["perf_score"], 90)
        self.assertEqual(store.analyzing_queue_get(), [])

    def test_stale_analysis_result_is_not_reported_successful(self):
        self.inline_jobs()
        def change_plan(row, **kwargs):
            store.save_plan(self.qid, PLAN + "\n| 1 | TABLE ACCESS FULL | T |")
            return RESULT
        with patch.object(api, "connect_oracle", return_value=self.connection()), \
                patch("collector.oracle_collector.get_execution_plan", return_value=PLAN), \
                patch("analyzer.ai_analyzer.analyze_query", side_effect=change_plan):
            response = self.client.get(f"/api/analyze/{self.qid}/stream")
            self.assertIn('"type": "error"', response.text)
            self.assertNotIn('"type": "done"', response.text)
            self.assertFalse(store.get_query_detail(self.qid)["query"]["analyzed"])
            self.assertIsNone(store.get_query_detail(self.qid)["query"]["analysis_error"])
            self.assertIn(self.qid, [row["id"] for row in store.get_unanalyzed()])

    def test_failure_of_old_plan_does_not_block_automatic_analysis_of_new_plan(self):
        self.inline_jobs()
        def failed_old_version(row, **kwargs):
            store.save_plan(self.qid, PLAN + "\n| 1 | TABLE ACCESS FULL | NEW_TABLE |")
            raise ValueError("Synthetic failure of old version")
        with patch.object(api, "connect_oracle", return_value=self.connection()), \
                patch("collector.oracle_collector.get_execution_plan", return_value=PLAN), \
                patch("analyzer.ai_analyzer.analyze_query", side_effect=failed_old_version):
            response = self.client.get(f"/api/analyze/{self.qid}/stream")
        self.assertIn('"type": "error"', response.text)
        self.assertIsNone(store.get_query_detail(self.qid)["query"]["analysis_error"])
        self.assertIn(self.qid, [row["id"] for row in store.get_unanalyzed()])

    def test_executor_rejection_releases_reservation(self):
        with patch.object(api._analysis_executor, "submit", side_effect=RuntimeError("shutdown")):
            self.assertEqual(self.client.post(f"/api/analyze/{self.qid}").status_code, 503)
        self.assertEqual(store.analyzing_queue_get(), [])
        self.assertNotIn(self.qid, api.analyzing_ids)

    def test_claim_distinguishes_full_queue_from_deleted_query(self):
        with patch("db.store.analyzing_queue_add", side_effect=ValueError("File pleine")):
            self.assertEqual(self.client.post(f"/api/analyze/{self.qid}").status_code, 429)
        def deleted(query_id):
            store.delete_query_data(query_id)
            raise ValueError("Requete introuvable")
        with patch("db.store.analyzing_queue_add", side_effect=deleted):
            self.assertEqual(self.client.post(f"/api/analyze/{self.qid}").status_code, 404)
        self.assertEqual(store.analyzing_queue_get(), [])

    def test_duplicate_start_is_atomic(self):
        with patch.object(api._analysis_executor, "submit") as submit:
            barrier = threading.Barrier(3)
            results = []
            def start():
                barrier.wait()
                results.append(api._start_analysis(self.qid))
            threads = [threading.Thread(target=start) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2)
            self.assertEqual(sorted(results), [False, True])
            submit.assert_called_once()
        api._finish_analysis(self.qid, {"type": "error", "message": "Fixture cleanup"})

    def test_post_and_bulk_subscribers_receive_terminal_event(self):
        for bulk in (False, True):
            with patch.object(api._analysis_executor, "submit") as submit:
                endpoint = "/api/analyze/all" if bulk else f"/api/analyze/{self.qid}"
                self.assertEqual(self.client.post(endpoint, json={}).status_code, 200)
                subscriber = queue.Queue(maxsize=128)
                with api._analysis_lock:
                    api._stream_queues.setdefault(self.qid, []).append(subscriber)
                with patch.object(api, "connect_oracle", side_effect=RuntimeError("offline")), \
                        patch("analyzer.ai_analyzer.analyze_query", return_value=RESULT):
                    function, *args = submit.call_args.args
                    function(*args)
                self.assertIn("done", [subscriber.get_nowait()["type"] for _ in range(subscriber.qsize())])
                self.assertEqual(store.analyzing_queue_get(), [])
            connection = store.get_conn()
            connection.execute("UPDATE queries SET analyzed=0 WHERE id=?", (self.qid,))
            connection.commit()
            connection.close()

    def test_auto_subscription_ignores_old_result_and_terminates(self):
        store.save_analysis(self.qid, RESULT)
        store.analyzing_queue_add(self.qid)
        request = MagicMock()
        async def connected():
            return False
        request.is_disconnected = connected
        async def scenario():
            response = await api.analyze_query_stream(self.qid, request)
            store.analyzing_queue_remove(self.qid)
            return "".join([part async for part in response.body_iterator])
        text = asyncio.run(scenario())
        self.assertIn('"type": "error"', text)
        self.assertNotIn('"type": "done"', text)
        self.assertNotIn(self.qid, api._stream_queues)

    def test_auto_subscription_reports_new_result(self):
        store.analyzing_queue_add(self.qid)
        request = MagicMock()
        async def connected():
            return False
        request.is_disconnected = connected
        async def scenario():
            response = await api.analyze_query_stream(self.qid, request)
            store.save_analysis(self.qid, RESULT)
            store.analyzing_queue_remove(self.qid)
            return "".join([part async for part in response.body_iterator])
        self.assertIn('"type": "done"', asyncio.run(scenario()))

    def test_chat_concurrency_and_clear_are_rejected_until_turn_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        def turn(*args):
            entered.set()
            release.wait(2)
            return {"reply": "fixture"}
        with patch.object(api, "_post_chat_turn", side_effect=turn):
            thread = threading.Thread(target=api._post_chat, args=(self.qid, {"message": "one"}))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(HTTPException) as caught:
                    api._post_chat(self.qid, {"message": "two"})
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(self.client.delete(f"/api/queries/{self.qid}/chat").status_code, 409)
            finally:
                release.set()
                thread.join(timeout=2)
        self.assertNotIn(self.qid, api._chat_ids)

    def test_chat_masks_initial_context_history_and_tool_results(self):
        import json
        connection = store.get_conn()
        connection.execute("UPDATE queries SET sql_text=? WHERE id=?",
                           ("select 'synthetic-private' from dual where id=7788", self.qid))
        connection.commit()
        connection.close()
        store.chat_add_message(self.qid, "user", "WHERE token='old-private'")
        calls = [{"id": "call-1", "name": "bind_captures", "arguments": {"sql_id": "fixture"}}]
        with patch.object(api, "connect_oracle", return_value=self.connection()), \
                patch("analyzer.oracle_tools.get_tools_schema_filtered", return_value=[]), \
                patch("analyzer.oracle_tools.execute_tool_native", return_value={
                    "binds": [{"value": "captured-private", "bind_name": ":id"}]}) as execute, \
                patch.object(api._copilot_client, "chat_with_tools",
                             side_effect=[("Thinking", calls, {}), ("Final fixture", [], {})]) as chat:
            response = self.client.post(f"/api/queries/{self.qid}/chat",
                                        json={"message": "WHERE token='message-private'"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["reply"], "Final fixture")
            payload = json.dumps([call.kwargs["messages"] for call in chat.call_args_list])
            for secret in ("synthetic-private", "old-private", "captured-private", "message-private", "7788"):
                self.assertNotIn(secret, payload)
            self.assertIn("deadline", execute.call_args.kwargs)
            self.assertEqual(execute.call_args.args[2]["child_number"], 2)
            self.assertEqual([row["role"] for row in store.chat_get_messages(self.qid)],
                             ["user", "user", "assistant"])

    def test_chat_raw_values_require_explicit_setting_and_empty_answer_is_failure(self):
        store.set_setting("ai_send_raw_values", "true")
        with patch.object(api, "connect_oracle", return_value=self.connection()), \
                patch("analyzer.oracle_tools.get_tools_schema_filtered", return_value=[]), \
                patch.object(api._copilot_client, "chat_with_tools", return_value=("", [], {})) as chat:
            response = self.client.post(f"/api/queries/{self.qid}/chat",
                                        json={"message": "WHERE token='explicit-value'"})
            self.assertEqual(response.status_code, 502)
            self.assertIn("explicit-value", chat.call_args.kwargs["messages"][-1]["content"])
            self.assertEqual(store.chat_get_messages(self.qid), [])

    def test_sse_timeout_unsubscribes_without_stealing_external_reservation(self):
        store.analyzing_queue_add(self.qid)
        with patch.object(api, "_STREAM_TIMEOUT", 0):
            response = self.client.get(f"/api/analyze/{self.qid}/stream")
        self.assertIn('"type": "error"', response.text)
        self.assertIn(self.qid, store.analyzing_queue_get())
        self.assertNotIn(self.qid, api._stream_queues)

    def test_event_adapter_preserves_ui_wire_contract(self):
        subscriber = queue.Queue(maxsize=128)
        api._stream_queues[self.qid] = [subscriber]
        api._analysis_event(self.qid, {"type": "tool_start", "tool": "fixture", "args": {"id": 1}})
        self.assertEqual(subscriber.get_nowait(), {"type": "tool_call", "name": "fixture", "args": {"id": 1}})
        api._analysis_event(self.qid, {"type": "tool_result", "tool": "fixture", "result": {"ok": 1},
                                      "ok": True, "ms": 3})
        event = subscriber.get_nowait()
        self.assertEqual((event["type"], event["name"], event["ok"], event["ms"]),
                         ("tool_result", "fixture", True, 3))
        api._analysis_event(self.qid, {"type": "complete", "result": RESULT})
        self.assertEqual(subscriber.get_nowait(), {"type": "analysis", "content": RESULT["raw"]})


if __name__ == "__main__":
    unittest.main()
