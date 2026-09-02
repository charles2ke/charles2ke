#!/usr/bin/env python3
"""Unit tests for scripts/agent_session_mcp.py."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Make the scripts package importable without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agent_session_mcp import (
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    TOOLS,
    ToolError,
    call_tool,
    create_agent_session,
    get_agent_session,
    get_token,
    handle_request,
    list_agent_sessions,
    serve,
    split_repository,
)


class FakeRequest:
    """Records requests and returns a canned response."""

    def __init__(self, response=None):
        self.calls: list[tuple[str, str, str, dict | None]] = []
        self.response = response if response is not None else {"id": "task_1"}

    def __call__(self, method, url, token, payload=None):
        self.calls.append((method, url, token, payload))
        return self.response


class SplitRepositoryTests(unittest.TestCase):
    def test_splits_owner_and_repo(self):
        self.assertEqual(split_repository("charles2ke/charles2ke"), ("charles2ke", "charles2ke"))

    def test_tolerates_surrounding_whitespace_and_slashes(self):
        self.assertEqual(split_repository(" /owner/repo/ "), ("owner", "repo"))

    def test_rejects_malformed_values(self):
        for value in ["", "owner", "owner/repo/extra", "owner/", "/repo", 42, None]:
            with self.subTest(value=value), self.assertRaises(ToolError):
                split_repository(value)

    def test_rejects_query_injection(self):
        with self.assertRaises(ToolError):
            split_repository("owner/repo?per_page=1")

    def test_escapes_unsafe_characters(self):
        self.assertEqual(split_repository("own er/re po"), ("own%20er", "re%20po"))


class TokenTests(unittest.TestCase):
    def test_prefers_agent_token(self):
        with patch.dict("os.environ", {"GITHUB_AGENT_TOKEN": "a", "GITHUB_TOKEN": "b"}):
            self.assertEqual(get_token(), "a")

    def test_falls_back_to_other_variables(self):
        with patch.dict("os.environ", {"GH_TOKEN": "c"}, clear=True):
            self.assertEqual(get_token(), "c")

    def test_errors_without_token(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ToolError):
            get_token()


class CreateAgentSessionTests(unittest.TestCase):
    def test_posts_prompt_to_repository_tasks_endpoint(self):
        fake = FakeRequest({"id": "task_1", "state": "queued"})
        with patch("scripts.agent_session_mcp.request", fake):
            result = create_agent_session(
                {"repository": "charles2ke/charles2ke", "prompt": "Fix the login button"},
                "token",
            )

        self.assertEqual(result, {"id": "task_1", "state": "queued"})
        method, url, token, payload = fake.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(
            url, "https://api.github.com/agents/repos/charles2ke/charles2ke/tasks"
        )
        self.assertEqual(token, "token")
        self.assertEqual(payload, {"prompt": "Fix the login button"})

    def test_forwards_optional_parameters(self):
        fake = FakeRequest()
        with patch("scripts.agent_session_mcp.request", fake):
            create_agent_session(
                {
                    "repository": "owner/repo",
                    "prompt": "Do the thing",
                    "base_ref": "main",
                    "model": "auto",
                    "create_pull_request": True,
                },
                "token",
            )

        self.assertEqual(
            fake.calls[0][3],
            {
                "prompt": "Do the thing",
                "base_ref": "main",
                "model": "auto",
                "create_pull_request": True,
            },
        )

    def test_requires_a_prompt(self):
        for arguments in [
            {"repository": "owner/repo"},
            {"repository": "owner/repo", "prompt": "   "},
            {"repository": "owner/repo", "prompt": 5},
        ]:
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                create_agent_session(arguments, "token")

    def test_rejects_wrongly_typed_options(self):
        with self.assertRaises(ToolError):
            create_agent_session(
                {"repository": "owner/repo", "prompt": "x", "base_ref": 1}, "token"
            )
        with self.assertRaises(ToolError):
            create_agent_session(
                {"repository": "owner/repo", "prompt": "x", "create_pull_request": "yes"},
                "token",
            )


class ReadAgentSessionTests(unittest.TestCase):
    def test_get_uses_session_id(self):
        fake = FakeRequest({"state": "in_progress"})
        with patch("scripts.agent_session_mcp.request", fake):
            get_agent_session({"repository": "owner/repo", "session_id": "abc/1"}, "token")

        method, url, _, payload = fake.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://api.github.com/agents/repos/owner/repo/tasks/abc%2F1")
        self.assertIsNone(payload)

    def test_get_requires_session_id(self):
        with self.assertRaises(ToolError):
            get_agent_session({"repository": "owner/repo"}, "token")
        with self.assertRaises(ToolError):
            get_agent_session({"repository": "owner/repo", "session_id": 1}, "token")

    def test_list_for_repository(self):
        fake = FakeRequest([])
        with patch("scripts.agent_session_mcp.request", fake):
            list_agent_sessions({"repository": "owner/repo"}, "token")

        self.assertEqual(fake.calls[0][1], "https://api.github.com/agents/repos/owner/repo/tasks")

    def test_list_without_repository(self):
        fake = FakeRequest([])
        with patch("scripts.agent_session_mcp.request", fake):
            list_agent_sessions({}, "token")

        self.assertEqual(fake.calls[0][1], "https://api.github.com/agents/tasks")


class CallToolTests(unittest.TestCase):
    def test_returns_json_text_content(self):
        fake = FakeRequest({"id": "task_1"})
        with (
            patch("scripts.agent_session_mcp.request", fake),
            patch.dict("os.environ", {"GITHUB_TOKEN": "t"}),
        ):
            result = call_tool(
                "create_agent_session", {"repository": "owner/repo", "prompt": "hi"}
            )

        self.assertNotIn("isError", result)
        self.assertEqual(json.loads(result["content"][0]["text"]), {"id": "task_1"})

    def test_reports_tool_errors_as_error_content(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "t"}):
            result = call_tool("create_agent_session", {"repository": "bad", "prompt": "hi"})

        self.assertTrue(result["isError"])
        self.assertIn("owner/repo", result["content"][0]["text"])

    def test_unknown_tool(self):
        with self.assertRaises(ToolError):
            call_tool("nope", {})


class ProtocolTests(unittest.TestCase):
    def test_initialize(self):
        response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(response["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertIn("tools", response["result"]["capabilities"])

    def test_tools_list_exposes_documented_tools(self):
        response = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(
            names, ["create_agent_session", "get_agent_session", "list_agent_sessions"]
        )
        for tool in TOOLS:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_notifications_get_no_response(self):
        self.assertIsNone(handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method(self):
        response = handle_request({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(response["error"]["code"], METHOD_NOT_FOUND)

    def test_tools_call_requires_params(self):
        response = handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call"})
        self.assertIn("error", response)

    def test_malformed_tools_call_notifications_get_no_response(self):
        for params in [
            None,
            {},
            {"name": "create_agent_session", "arguments": []},
            {"name": "unknown", "arguments": {}},
        ]:
            message = {"jsonrpc": "2.0", "method": "tools/call"}
            if params is not None:
                message["params"] = params
            with self.subTest(params=params):
                self.assertIsNone(handle_request(message))

    def test_tools_call_dispatches(self):
        fake = FakeRequest({"id": "task_9"})
        with (
            patch("scripts.agent_session_mcp.request", fake),
            patch.dict("os.environ", {"GITHUB_TOKEN": "t"}),
        ):
            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "create_agent_session",
                        "arguments": {"repository": "owner/repo", "prompt": "hi"},
                    },
                }
            )

        self.assertEqual(json.loads(response["result"]["content"][0]["text"]), {"id": "task_9"})


class ServeTests(unittest.TestCase):
    def test_serves_requests_and_skips_notifications(self):
        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
            + "\n"
            + "not json\n"
        )
        stdout = io.StringIO()

        self.assertEqual(serve(stdin, stdout), 0)

        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[1]["error"]["code"], PARSE_ERROR)


if __name__ == "__main__":
    unittest.main()
