#!/usr/bin/env python3
"""A Model Context Protocol (MCP) server for GitHub coding agent sessions.

The server speaks MCP over stdio (newline-delimited JSON-RPC 2.0) and exposes
three tools backed by the GitHub agent tasks REST API:

- ``create_agent_session`` — start a new agent session (task) on a repository.
- ``get_agent_session`` — read the state of an existing session.
- ``list_agent_sessions`` — list sessions for a repository, or for every
  repository the token can reach.

Authentication uses a user-to-server token read from ``GITHUB_AGENT_TOKEN``,
``GITHUB_TOKEN`` or ``GH_TOKEN``. Server-to-server tokens (such as GitHub App
installation tokens) are not supported by the agent tasks API.

Run it directly for a manual smoke test::

    GITHUB_TOKEN=... python scripts/agent_session_mcp.py

or wire it into an MCP client, for example::

    {
      "mcpServers": {
        "github-agent-sessions": {
          "command": "python",
          "args": ["scripts/agent_session_mcp.py"],
          "env": {"GITHUB_TOKEN": "${input:github_token}"}
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import IO, Any

API_ROOT = "https://api.github.com"
USER_AGENT = "charles2ke-agent-session-mcp"
API_VERSION = "2022-11-28"
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "github-agent-sessions"
SERVER_VERSION = "1.0.0"
TOKEN_ENV_VARS = ("GITHUB_AGENT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")

# JSON-RPC error codes (https://www.jsonrpc.org/specification).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

REPOSITORY_SCHEMA = {
    "type": "string",
    "description": "Repository in 'owner/repo' form, e.g. 'charles2ke/charles2ke'.",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "create_agent_session",
        "description": (
            "Start a GitHub coding agent session (agent task) for a repository. "
            "The agent works on the repository in GitHub's cloud and reports back "
            "through a pull request."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": REPOSITORY_SCHEMA,
                "prompt": {
                    "type": "string",
                    "description": "The task for the agent to work on.",
                },
                "base_ref": {
                    "type": "string",
                    "description": "Base branch for the agent's branch and pull request.",
                },
                "model": {
                    "type": "string",
                    "description": "Model to use. Omit for automatic model selection.",
                },
                "create_pull_request": {
                    "type": "boolean",
                    "description": "Whether the agent should open a pull request.",
                },
            },
            "required": ["repository", "prompt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_agent_session",
        "description": "Get the state of an existing GitHub coding agent session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": REPOSITORY_SCHEMA,
                "session_id": {
                    "type": "string",
                    "description": "Identifier of the agent session (task) to look up.",
                },
            },
            "required": ["repository", "session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_agent_sessions",
        "description": (
            "List GitHub coding agent sessions for a repository, or across every "
            "repository the token can access when no repository is given."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"repository": REPOSITORY_SCHEMA},
            "additionalProperties": False,
        },
    },
]


class ToolError(Exception):
    """Raised when a tool call cannot be completed."""


def split_repository(repository: Any) -> tuple[str, str]:
    """Split ``owner/repo`` into its two parts, rejecting malformed input."""
    if not isinstance(repository, str):
        raise ToolError("'repository' must be a string in 'owner/repo' form.")

    parts = repository.strip().strip("/").split("/")
    if len(parts) != 2 or not all(parts):
        raise ToolError(f"Invalid repository {repository!r}: expected 'owner/repo'.")

    owner, repo = parts
    if any(character in part for part in parts for character in "?#"):
        raise ToolError(f"Invalid repository {repository!r}: expected 'owner/repo'.")

    return urllib.parse.quote(owner, safe=""), urllib.parse.quote(repo, safe="")


def get_token() -> str:
    """Return the GitHub token used to call the agent tasks API."""
    for name in TOKEN_ENV_VARS:
        token = os.getenv(name)
        if token:
            return token
    raise ToolError(
        "No GitHub token found. Set one of: " + ", ".join(TOKEN_ENV_VARS) + "."
    )


def request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    """Send a request to the GitHub API and return the decoded JSON body."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
        "Authorization": "Bearer " + token,
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    http_request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(http_request, timeout=60) as response:
            raw = response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace").strip()
        message = f"GitHub API request failed: {error.code} {error.reason}"
        raise ToolError(f"{message}: {detail}" if detail else message) from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise ToolError(f"GitHub API request failed: {reason}") from error

    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ToolError("GitHub API request failed: invalid JSON response") from error


def create_agent_session(arguments: dict[str, Any], token: str) -> Any:
    """Start a new agent session for a repository."""
    owner, repo = split_repository(arguments.get("repository"))

    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolError("'prompt' is required and must be a non-empty string.")

    payload: dict[str, Any] = {"prompt": prompt}
    for name in ("base_ref", "model"):
        value = arguments.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise ToolError(f"'{name}' must be a string.")
            payload[name] = value

    create_pull_request = arguments.get("create_pull_request")
    if create_pull_request is not None:
        if not isinstance(create_pull_request, bool):
            raise ToolError("'create_pull_request' must be a boolean.")
        payload["create_pull_request"] = create_pull_request

    url = f"{API_ROOT}/agents/repos/{owner}/{repo}/tasks"
    return request("POST", url, token, payload)


def get_agent_session(arguments: dict[str, Any], token: str) -> Any:
    """Return the current state of an agent session."""
    owner, repo = split_repository(arguments.get("repository"))

    session_id = arguments.get("session_id")
    if not isinstance(session_id, str):
        raise ToolError("'session_id' is required and must be a string.")
    session_id = session_id.strip()
    if not session_id:
        raise ToolError("'session_id' is required and must be a string.")

    quoted = urllib.parse.quote(session_id, safe="")
    url = f"{API_ROOT}/agents/repos/{owner}/{repo}/tasks/{quoted}"
    return request("GET", url, token)


def list_agent_sessions(arguments: dict[str, Any], token: str) -> Any:
    """List agent sessions for a repository, or for every reachable repository."""
    repository = arguments.get("repository")
    if repository is None:
        url = f"{API_ROOT}/agents/tasks"
    else:
        owner, repo = split_repository(repository)
        url = f"{API_ROOT}/agents/repos/{owner}/{repo}/tasks"
    return request("GET", url, token)


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], str], Any]] = {
    "create_agent_session": create_agent_session,
    "get_agent_session": get_agent_session,
    "list_agent_sessions": list_agent_sessions,
}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a tool and return an MCP ``tools/call`` result."""
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool: {name}")

    try:
        result = handler(arguments, get_token())
    except ToolError as error:
        return {
            "content": [{"type": "text", "text": str(error)}],
            "isError": True,
        }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message, returning a response when one is needed."""
    method = message.get("method")
    message_id = message.get("id")
    is_notification = "id" not in message

    def error_or_no_response(code: int, error_message: str) -> dict[str, Any] | None:
        if is_notification:
            return None
        return error_response(message_id, code, error_message)

    if not isinstance(method, str):
        return error_or_no_response(INVALID_REQUEST, "Missing method.")

    if method == "initialize":
        result: Any = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "ping":
        result = {}
    elif method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict):
            return error_or_no_response(INVALID_PARAMS, "Missing params.")
        name = params.get("name")
        if not isinstance(name, str):
            return error_or_no_response(INVALID_PARAMS, "Missing tool name.")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return error_or_no_response(INVALID_PARAMS, "'arguments' must be an object.")
        try:
            result = call_tool(name, arguments)
        except ToolError as error:
            return error_or_no_response(INVALID_PARAMS, str(error))
        except Exception as error:  # noqa: BLE001 - never kill the server loop
            return error_or_no_response(INTERNAL_ERROR, f"Tool failed: {error}")
    elif is_notification:
        # Notifications we don't handle (e.g. notifications/initialized).
        return None
    else:
        return error_response(message_id, METHOD_NOT_FOUND, f"Unknown method: {method}")

    if is_notification:
        return None
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def error_response(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def serve(stdin: IO[str], stdout: IO[str]) -> int:
    """Read newline-delimited JSON-RPC messages until stdin is closed."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = error_response(None, PARSE_ERROR, "Invalid JSON.")
        else:
            if not isinstance(message, dict):
                response = error_response(None, INVALID_REQUEST, "Expected a JSON object.")
            else:
                response = handle_request(message)

        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
    return 0


def main() -> int:
    return serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
