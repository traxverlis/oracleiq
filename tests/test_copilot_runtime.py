import asyncio
import json
import os
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from copilot import CopilotClient
from analyzer import copilot_client
from db import store


@unittest.skipUnless(os.getenv("ODIN_TEST_COPILOT_RUNTIME") == "1", "Runtime integration opt-in")
class CopilotRuntimeTests(unittest.TestCase):
    def setUp(self):
        database = tempfile.TemporaryDirectory()
        self.addCleanup(database.cleanup)
        path_patch = patch.object(store, "DB_PATH", Path(database.name) / "runtime.db")
        path_patch.start()
        self.addCleanup(path_patch.stop)
        store.init_db()

    def test_real_runtime_text_and_terminal_tool(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                tools = body.get("tools", [])
                message = {"role": "assistant", "content": "SCORE: 75"}
                finish = "stop"
                if tools:
                    message = {"role": "assistant", "content": None, "tool_calls": [{
                        "index": 0, "id": "call-fixture", "type": "function", "function": {
                            "name": tools[0]["function"]["name"], "arguments": '{"sql_id":"fixture"}'}}]}
                    finish = "tool_calls"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
                self.end_headers()
                if body.get("stream"):
                    for delta, reason in ((message, None), ({}, finish)):
                        event = {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                                 "model": "fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
                        self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.wfile.write(json.dumps({"id": "fixture", "object": "chat.completion", "created": 1,
                        "model": "fixture", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        @asynccontextmanager
        async def local_client(token):
            with tempfile.TemporaryDirectory(prefix="odin-sdk-test-") as directory:
                runtime_env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR")
                               if key in os.environ}
                runtime_env.update(
                    HOME=directory, USERPROFILE=directory, TEMP=directory, TMP=directory,
                    TMPDIR=directory, APPDATA=directory, LOCALAPPDATA=directory,
                    COPILOT_OFFLINE="true",
                )
                async with CopilotClient(
                    mode="empty", use_logged_in_user=False, working_directory=directory,
                    base_directory=directory, env=runtime_env, log_level="error",
                ) as client:
                    create = client.create_session

                    async def create_local(**options):
                        self.assertEqual(options.pop("github_token"), "fixture")
                        options["provider"] = {"type": "openai", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                               "api_key": "local-fixture", "wire_api": "completions"}
                        return await create(**options)

                    with patch.object(client, "create_session", side_effect=create_local):
                        yield client

        async def run():
            history = [{"role": "user", "content": "Analyse fixture"}]
            with patch.object(copilot_client, "_client", local_client):
                text, calls, usage = await copilot_client._chat("fixture", history, [], "fixture", 1000, None, "none", None)
                self.assertIn("SCORE: 75", text)
                self.assertEqual(calls, [])
                schema = [{"type": "function", "function": {"name": "oracle_plan", "description": "Oracle plan",
                           "parameters": {"type": "object", "properties": {"sql_id": {"type": "string"}},
                                          "required": ["sql_id"]}}}]
                text, calls, usage = await copilot_client._chat("fixture", history, schema, "fixture", 1000, None, "required", None)
                self.assertEqual(calls, [{"id": "call-fixture", "name": "oracle_plan", "arguments": {"sql_id": "fixture"}}])
                self.assertEqual(len(requests), 2)
                self.assertEqual(len(requests[1]["tools"]), 1)

        try:
            asyncio.run(asyncio.wait_for(run(), timeout=60))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()