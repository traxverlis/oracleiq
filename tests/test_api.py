import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_database = tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".test-api-")
os.environ["ODIN_DB_PATH"] = str(Path(_database.name) / "test.db")

from fastapi.testclient import TestClient
from db import store
store.DB_PATH = Path(os.environ["ODIN_DB_PATH"])
from api.app import app
from api import security


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".test-api-case-")
        self.addCleanup(self.database.cleanup)
        self.db_patch = patch.object(store, "DB_PATH", Path(self.database.name) / "test.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        store.init_db()
        security._attempts.clear()
        from api.app import _analysis_lock, _analysis_results, _stream_queues, analyzing_ids
        with _analysis_lock:
            _analysis_results.clear()
            _stream_queues.clear()
            analyzing_ids.clear()
        self.client = TestClient(app)
        self.client.cookies.set(security.COOKIE_NAME, security.make_token())

    def tearDown(self):
        self.client.close()

    def test_sensitive_routes_require_admin(self):
        self.client.cookies.clear()
        for path in ("/api/settings/collector_active", "/api/settings/test_oracle",
                     "/api/queries/1/replay", "/api/analyze/1", "/api/queries/1/chat"):
            self.assertEqual(self.client.post(path, json={}).status_code, 403, path)

    def test_github_token_management_saves_tests_and_deletes_without_leaking_secret(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/settings/github_token").status_code, 401)
        self.client.cookies.set(security.COOKIE_NAME, security.make_token())
        with patch("api.app._copilot_client.github_token_source", return_value=None), \
             patch("api.app._copilot_client.has_github_token", return_value=False):
            response = self.client.get("/api/settings/github_token")
            self.assertEqual(response.json(), {"configured": False, "source": None})

        with patch("api.app._copilot_client.github_token_source", return_value="file") as save_source, \
             patch("api.app._copilot_client.save_github_token") as save, \
             patch("api.app.set_setting") as set_setting_mock:
            response = self.client.post("/api/settings/github_token", json={"token": "super-secret-value"})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("super-secret-value", response.text)
            save.assert_called_once_with("super-secret-value")
            set_setting_mock.assert_any_call("copilot_model_catalog", '{"models":[],"updated_at":null}')

        with patch("api.app._copilot_client.github_token_source", return_value="env"):
            response = self.client.post("/api/settings/github_token", json={"token": "x"})
            self.assertEqual(response.status_code, 409)
            response = self.client.delete("/api/settings/github_token")
            self.assertEqual(response.status_code, 409)

        with patch("api.app._copilot_client.github_token_source", return_value="file"), \
             patch("api.app._copilot_client.delete_github_token") as delete, \
             patch("api.app.set_setting") as set_setting_mock:
            response = self.client.delete("/api/settings/github_token")
            self.assertEqual(response.status_code, 200)
            delete.assert_called_once()
            set_setting_mock.assert_any_call("copilot_model_catalog", '{"models":[],"updated_at":null}')

        with patch("api.app._copilot_client.test_github_token") as test:
            response = self.client.post("/api/settings/github_token/test", json={"token": "candidate"})
            self.assertEqual(response.json(), {"ok": True})
            test.assert_called_once_with("candidate")
        from analyzer.copilot_client import CopilotAuthenticationError
        with patch("api.app._copilot_client.test_github_token", side_effect=CopilotAuthenticationError("Echange du jeton Copilot refuse (HTTP 404). Acces direct au catalogue Copilot refuse (HTTP 403).")):
            response = self.client.post("/api/settings/github_token/test", json={})
            self.assertFalse(response.json()["ok"])
            self.assertIn("HTTP 404", response.json()["error"])
            self.assertIn("HTTP 403", response.json()["error"])
        with patch("api.app._copilot_client.test_github_token", side_effect=RuntimeError("private-secret")):
            response = self.client.post("/api/settings/github_token/test", json={})
            self.assertFalse(response.json()["ok"])
            self.assertNotIn("private-secret", response.text)
        with patch("api.app._copilot_client.test_github_token", side_effect=ConnectionError("connect timeout to 10.0.0.1 with secret-token-abc")):
            response = self.client.post("/api/settings/github_token/test", json={})
            self.assertEqual(response.json()["ok"], False)
            self.assertNotIn("secret-token-abc", response.text)
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/settings/github_token/test", json={}).status_code, 403)

    def test_health_is_admin_only_even_with_public_read(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertIn("collector", response.json()["services"])
        self.assertNotIn("oracle_password", response.text)
        with patch.object(security, "PUBLIC_READ", True):
            self.client.cookies.clear()
            self.assertEqual(self.client.get("/api/health").status_code, 403)
            self.assertEqual(self.client.get("/api/settings").status_code, 403)
            self.client.cookies.set(security.COOKIE_NAME, security.make_token("viewer"))
            self.assertEqual(self.client.get("/api/health").status_code, 403)
            self.assertEqual(self.client.get("/api/settings").status_code, 403)

    def test_model_catalog_refresh_preserves_settings_and_cache_on_failure(self):
        import json
        from analyzer.copilot_client import CopilotAuthenticationError
        catalog = [{"id": "new-model", "name": "New model", "vendor": "Vendor"}]
        with patch("config.AI_PROVIDER", "github-copilot"), patch("api.app.set_setting") as save:
            with patch("api.app._copilot_client.list_account_models", return_value=catalog):
                response = self.client.post("/api/models/refresh")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["models"], catalog)
                self.assertEqual(save.call_args.args[0], "copilot_model_catalog")
                self.assertEqual(json.loads(save.call_args.args[1])["models"], catalog)
            save.reset_mock()
            with patch("api.app._copilot_client.list_account_models", side_effect=RuntimeError("private token")):
                response = self.client.post("/api/models/refresh")
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("private token", response.text)
                save.assert_not_called()
            with patch("api.app._copilot_client.list_account_models", side_effect=CopilotAuthenticationError(
                    "GitHub refuse l'acces au compte (403). Verifiez la permission Copilot Requests.")):
                response = self.client.post("/api/models/refresh")
                self.assertEqual(response.status_code, 502)
                self.assertIn("403", response.json()["detail"])
                self.assertIn("Copilot Requests", response.json()["detail"])
                save.assert_not_called()
        with patch.object(security, "PUBLIC_READ", True):
            self.client.cookies.clear()
            self.assertEqual(self.client.get("/api/models").status_code, 403)
            self.assertEqual(self.client.post("/api/models/refresh").status_code, 403)

    def test_pages_render_with_installed_starlette(self):
        for path in ("/settings/login", "/", "/settings"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)

    def test_only_native_settings_and_prompt_are_exposed(self):
        with patch("api.app.get_setting", side_effect=lambda key, default=None: "classic" if key == "analyzer_ai_mode" else default):
            settings = self.client.get("/api/settings").json()
            self.assertEqual(settings["analyzer_ai_mode"], "native")
            self.assertNotIn("tool_rounds", settings)
            self.assertNotIn("thinking_budget", settings)
        self.assertEqual(set(self.client.get("/api/default_prompts").json()), {"native"})

    def test_settings_validation_and_password_redaction(self):
        for mode in ("classic", "agentic"):
            self.assertEqual(self.client.post("/api/settings", json={"analyzer_ai_mode": mode}).status_code, 422)
        self.assertEqual(self.client.post("/api/settings", json={"thinking_budget": 1024}).status_code, 422)
        self.assertEqual(self.client.post("/api/settings", json={"tool_rounds": -1}).status_code, 422)
        self.assertEqual(self.client.post("/api/settings", json={"analyzer_mode": "invalid"}).status_code, 422)
        with patch("api.app.set_settings") as save:
            result = self.client.post("/api/settings", json={"oracle_password": "synthetic", "gather_stats_enabled": False})
            self.assertEqual(result.status_code, 200)
            self.assertNotIn("synthetic", result.text)
            save.assert_called_once_with({"oracle_password": "synthetic", "gather_stats_enabled": "false"})

    def test_bulk_analysis_uses_bounded_executor(self):
        query_id = store.upsert_query({"sql_id": "bulk1", "sql_text": "select 1 from dual", "sql_hash": "bulk1", "schema_name": "APP"})
        from api.app import analyzing_ids
        try:
            with patch("api.app._analysis_executor.submit") as submit:
                result = self.client.post("/api/analyze/all", json={})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["queued"], 1)
                submit.assert_called_once()
                self.assertEqual(self.client.post(f"/api/analyze/{query_id}").json()["status"], "already_analyzing")
                submit.assert_called_once()
                self.assertEqual(self.client.delete(f"/api/queries/{query_id}").status_code, 409)
        finally:
            store.analyzing_queue_remove(query_id)
            analyzing_ids.discard(query_id)
            from api.app import _analysis_results
            _analysis_results.pop(query_id, None)
            store.delete_query_data(query_id)

    def test_native_stream_honors_settings_through_final_response(self):
        query_id = store.upsert_query({"sql_id": "native-stream", "sql_text": "select 1 from dual", "sql_hash": "native-stream"})
        settings = {"ai_model": "saved-model", "ai_max_tokens": "8000", "system_prompt": "Custom native"}
        lookup = lambda key, default=None: settings.get(key, default)
        responses = [("Preliminary", [], {}), ("SCORE: 75\nSEVERITY: warning\nSUMMARY: Test", [], {})]
        try:
            with patch("api.app.get_setting", side_effect=lookup), \
                    patch("db.store.get_setting", side_effect=lookup), \
                    patch("api.app.connect_oracle", side_effect=RuntimeError("Offline fixture")), \
                    patch("analyzer.oracle_tools.get_tools_schema_filtered", return_value=[]), \
                    patch("analyzer.copilot_client.chat_with_tools", side_effect=responses) as chat:
                response = self.client.get(f"/api/analyze/{query_id}/stream")
            self.assertEqual(response.status_code, 200)
            self.assertIn('"type": "done"', response.text)
            self.assertEqual(chat.call_count, 2)
            for invocation in chat.call_args_list:
                self.assertEqual(invocation.kwargs["model"], "saved-model")
                self.assertEqual(invocation.kwargs["max_tokens"], 8000)
                self.assertEqual(invocation.kwargs["system"], "Custom native")
                self.assertEqual(invocation.kwargs["tools"], [])
        finally:
            store.delete_query_data(query_id)

    def test_comparison_reads_validate_query_and_parameters(self):
        query_id = store.upsert_query({"sql_id": "comparison", "sql_text": "select 1 from dual", "sql_hash": "comparison"})
        try:
            self.assertEqual(self.client.get(f"/api/queries/{query_id}/performance?minutes=14").status_code, 422)
            self.assertEqual(self.client.get("/api/queries/-1/performance").status_code, 404)
            self.assertEqual(self.client.get(f"/api/queries/{query_id}/plan-comparison?before_id=1").status_code, 422)
            self.assertEqual(self.client.get(f"/api/queries/{query_id}/plan-comparison?before_id=999999&after_id=999998").status_code, 404)
            self.client.cookies.set(security.COOKIE_NAME, security.make_token("viewer"))
            self.assertEqual(self.client.get(f"/api/queries/{query_id}/performance?minutes=60").json()["current"]["state"], "insufficient")
            self.assertEqual(self.client.get(f"/api/queries/{query_id}/plan-comparison").json()["plans"], [])
        finally:
            store.delete_query_data(query_id)

    def test_bind_read_never_connects_and_refresh_requires_admin(self):
        query_id = store.upsert_query({"sql_id": "bindread", "sql_text": "select :id from dual", "sql_hash": "bindread"})
        try:
            with patch("api.app.connect_oracle") as connect, patch("api.app.assert_query_source"):
                response = self.client.get(f"/api/queries/{query_id}/binds")
                self.assertEqual(response.json()["source"], "sqlite")
                connect.assert_not_called()
                with patch("analyzer.oracle_tools.bind_captures", return_value={"captured": False, "binds": []}):
                    response = self.client.post(f"/api/queries/{query_id}/binds/refresh")
                    self.assertEqual(response.status_code, 200)
                    connect.assert_called_once()
                    connect.return_value.close.assert_called_once()
            self.client.cookies.set(security.COOKIE_NAME, security.make_token("viewer"))
            self.assertEqual(self.client.post(f"/api/queries/{query_id}/binds/refresh").status_code, 403)
        finally:
            store.delete_query_data(query_id)

    def test_refresh_targets_child_cursor(self):
        query_id = store.upsert_query({"sql_id": "childtest", "sql_text": "select 1 from dual", "sql_hash": "child", "schema_name": "APP", "child_number": 7})
        try:
            with patch("api.app.connect_oracle"), patch("api.app.assert_query_source"), patch("collector.oracle_collector.get_execution_plan", return_value="| Id | Operation | Name |\n| 0 | SELECT STATEMENT | |") as plan:
                response = self.client.post(f"/api/refresh_plan/{query_id}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(plan.call_args.args[2], 7)
        finally:
            store.delete_query_data(query_id)


if __name__ == "__main__":
    unittest.main()