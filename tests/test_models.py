import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from copilot.session_events import AssistantUsageData
from copilot.tools import ToolInvocation
from analyzer import copilot_client


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "token.json"
        self.addCleanup(patch.stopall)
        patch.object(copilot_client, "TOKEN_CACHE_PATH", self.cache).start()
        patch.dict(os.environ, {"GITHUB_TOKEN": ""}).start()
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.model = SimpleNamespace(id="responses-model", name="New model", policy=None,
                                     supported_reasoning_efforts=["low", "high"])
        self.client.list_models.return_value = [self.model]
        self.factory = patch.object(copilot_client, "CopilotClient", return_value=self.client).start()
        copilot_client.save_github_token("github_pat_fixture")

    def test_catalog_uses_official_sdk_and_explicit_identity(self):
        self.assertEqual(copilot_client.list_account_models(), [
            {"id": "responses-model", "name": "New model", "vendor": ""}])
        options = self.factory.call_args.kwargs
        self.assertEqual(options["github_token"], "github_pat_fixture")
        self.assertFalse(options["use_logged_in_user"])
        self.assertEqual(options["mode"], "empty")
        self.assertNotIn("GITHUB_TOKEN", options["env"])
        self.assertFalse(Path(options["base_directory"]).exists())
        self.client.__aexit__.assert_awaited_once()

    def test_catalog_filters_disabled_and_duplicate_models(self):
        disabled = SimpleNamespace(id="disabled", name="Disabled", policy=SimpleNamespace(state="disabled"))
        self.client.list_models.return_value = [self.model, disabled, self.model]
        self.assertEqual(len(copilot_client.list_account_models()), 1)

    def test_env_has_priority_and_test_candidate_does_not_save(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "github_pat_environment"}):
            copilot_client.list_account_models()
            self.assertEqual(self.factory.call_args.kwargs["github_token"], "github_pat_environment")
            copilot_client.test_github_token("github_pat_candidate")
            self.assertEqual(self.factory.call_args.kwargs["github_token"], "github_pat_candidate")
            self.assertEqual(copilot_client.github_token_source(), "env")
        self.assertEqual(copilot_client._load_cache(), {"oauth_token": "github_pat_fixture"})
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)
        copilot_client.delete_github_token()
        self.assertFalse(copilot_client.has_github_token())

    def test_missing_and_classic_tokens_never_start_runtime(self):
        copilot_client.delete_github_token()
        for token in (None, "ghp_fixture"):
            with self.assertRaises(copilot_client.CopilotAuthenticationError):
                copilot_client.test_github_token(token)
        self.factory.assert_not_called()

    def test_sdk_errors_and_timeouts_are_redacted_and_cleaned_up(self):
        for error in (RuntimeError("private-token"), TimeoutError("private-token")):
            self.client.list_models.side_effect = error
            with self.assertRaises(copilot_client.CopilotAuthenticationError) as raised:
                copilot_client.test_github_token()
            self.assertNotIn("private-token", str(raised.exception))
        self.assertEqual(self.client.__aexit__.await_count, 2)

    def test_empty_catalog_is_not_a_successful_token_test(self):
        self.client.list_models.return_value = []
        with self.assertRaises(copilot_client.CopilotAuthenticationError):
            copilot_client.test_github_token()

    def session(self, send):
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.send_and_wait.side_effect = send
        self.client.create_session.return_value = session
        return session

    def test_chat_preserves_history_system_and_usage(self):
        history = [{"role": "user", "content": "SQL"}, {"role": "assistant", "content": "Earlier"},
                   {"role": "tool", "tool_call_id": "call-1", "content": "Oracle result"}]

        async def send(prompt, **kwargs):
            self.assertEqual(json.loads(prompt), history)
            self.client.create_session.call_args.kwargs["on_event"](SimpleNamespace(
                data=AssistantUsageData(model="responses-model", input_tokens=10, output_tokens=5)))
            return SimpleNamespace(data=SimpleNamespace(content="SCORE: 75"))

        session = self.session(send)
        text, usage = copilot_client.chat(history, model="responses-model", system="Oracle system")
        self.assertEqual(text, "SCORE: 75")
        self.assertEqual(usage, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        options = self.client.create_session.call_args.kwargs
        self.assertIn("Oracle system", options["system_message"]["content"])
        self.assertEqual(options["reasoning_effort"], "low")
        self.assertEqual(options["available_tools"], [])
        self.assertEqual(options["excluded_tools"], ["builtin:*", "mcp:*"])
        self.assertFalse(options["enable_config_discovery"])
        self.assertFalse(options["enable_file_hooks"])
        self.assertFalse(options["enable_skills"])
        self.assertIsInstance(options["on_permission_request"](None, None), copilot_client.PermissionDecisionReject)
        session.__aexit__.assert_awaited_once()

    def test_tools_are_returned_to_odin_without_execution(self):
        schema = [{"type": "function", "function": {"name": "oracle_plan", "description": "Plan",
                   "parameters": {"type": "object", "properties": {"sql_id": {"type": "string"}}}}}]

        async def send(prompt, **kwargs):
            options = self.client.create_session.call_args.kwargs
            tool = options["tools"][0]
            self.assertTrue(tool.is_terminal)
            self.assertEqual(options["available_tools"], ["custom:oracle_plan"])
            result = await tool.handler(ToolInvocation(tool_call_id="call-1", tool_name="oracle_plan",
                                                      arguments={"sql_id": "fixture"}))
            self.assertEqual(result.result_type, "success")
            return None

        self.session(send)
        text, calls, usage = copilot_client.chat_with_tools([{"role": "user", "content": "SQL"}], schema)
        self.assertEqual(calls, [{"id": "call-1", "name": "oracle_plan", "arguments": {"sql_id": "fixture"}}])
        self.assertEqual(text, "")

    def test_sync_interface_can_be_called_from_running_event_loop(self):
        async def run():
            return copilot_client.list_account_models()
        self.assertEqual(asyncio.run(run())[0]["id"], "responses-model")