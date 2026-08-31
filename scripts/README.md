# Scripts

## `agent_session_mcp.py`

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an
MCP client start a GitHub coding agent session (an *agent task*) on any
repository you have access to. It speaks MCP over stdio using
newline-delimited JSON-RPC and calls the GitHub
[agent tasks REST API](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-via-the-api),
so it needs no third-party dependencies — just Python 3.

### Tools

- `create_agent_session` — start a session. Requires `repository`
  (`owner/repo`) and `prompt`; optionally takes `base_ref`, `model` and
  `create_pull_request`.
- `get_agent_session` — read the state of a session (`repository`,
  `session_id`). States include `queued`, `in_progress`, `completed`,
  `failed`, `idle`, `waiting_for_user`, `timed_out` and `cancelled`.
- `list_agent_sessions` — list sessions for a `repository`, or across every
  repository the token can reach when `repository` is omitted.

### Authentication

The server reads a token from `GITHUB_AGENT_TOKEN`, `GITHUB_TOKEN` or
`GH_TOKEN`, in that order. The agent tasks API only accepts user-to-server
tokens (a personal access token, an OAuth app token or a GitHub App
user-to-server token) — GitHub App installation tokens are not supported.

### Usage

Register it with an MCP client, for example in `.vscode/mcp.json` or another
client's server list:

```json
{
  "mcpServers": {
    "github-agent-sessions": {
      "command": "python",
      "args": ["scripts/agent_session_mcp.py"],
      "env": { "GITHUB_TOKEN": "${input:github_token}" }
    }
  }
}
```

You can also drive it by hand for a quick smoke test:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | python scripts/agent_session_mcp.py
```

Note that the agent tasks API is in public preview and may change.

## `collect_failures.py`

Builds the JSON snapshot behind the
[failure alerts dashboard](https://charles2ke.github.io/charles2ke/failures.html)
(`site/failures.html`). It walks every public, non-fork, non-archived
repository owned by `charles2ke`, reads their recent GitHub Actions runs and
keeps only the **unresolved** failures: the latest run of a workflow on a
branch, when that run ended in `failure`, `timed_out` or `startup_failure`. A
newer successful run of the same workflow on the same branch resolves the
earlier failure, so it drops off the dashboard automatically.

### Usage

```bash
python scripts/collect_failures.py --output _site/failures.json
```

Options:

- `--owner` — GitHub account to scan (defaults to `charles2ke`).
- `--output` — where to write the JSON snapshot (defaults to
  `_site/failures.json`).

The script reads an optional token from `ALERTS_TOKEN` or `GITHUB_TOKEN`.
Without a token it uses unauthenticated requests, which are rate limited to 60
per hour and are usually not enough to scan every repository.

The `Deploy to GitHub Pages` workflow runs this script on every deploy and on a
schedule, so the published dashboard keeps up with new failures. The page
itself re-fetches the snapshot once a minute.

## `set-topics.sh`

Applies a curated set of GitHub topics to every repository owned by
`charles2ke`. Repository topics are metadata (not files), so they can't be
set by committing a normal code change — this script exists to make that
one-time (or re-runnable) operation reviewable and repeatable instead of
requiring a manual visit to each repo's settings page.

### Prerequisites

- Bash 4 or later.
- For non-dry runs, [GitHub CLI (`gh`)](https://cli.github.com/) installed and
  authenticated (`gh auth login`) with a token that has at least the
  `public_repo` scope (use `repo` instead if any target repos are private).

### Usage

Always start with a dry run — it prints exactly what would be applied
without making any API calls or requiring authentication:

```bash
./scripts/set-topics.sh --dry-run
```

Once you're happy with the preview, apply the topics for real:

```bash
./scripts/set-topics.sh
```

Apply topics to a single repo only (dry run or for real):

```bash
./scripts/set-topics.sh --dry-run Nakshatra
./scripts/set-topics.sh Nakshatra
```

By default the script targets the `charles2ke` account. Override this with
the `OWNER` environment variable, e.g. `OWNER=some-org ./scripts/set-topics.sh`.

The topic list applied to each repo is defined near the top of
`set-topics.sh` in the `REPO_TOPICS` map — edit it there to add, remove, or
change topics. The script prints a success/failure line per repo, keeps
going even if one repo fails, and exits non-zero if any repo failed.

### Prefer not to run a script?

Topics can also be set manually, repo by repo: open the repo on GitHub,
click the gear icon next to **About** in the sidebar, and add topics in the
**Topics** field.
