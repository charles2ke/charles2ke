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

## `auto_release.py`

The engine behind the reusable
[`Auto release`](../.github/workflows/auto-release.yml) workflow. It answers
one question per run — *does this repository deserve a new release right
now?* — and only tags and publishes one when every gate agrees:

1. The default branch is ahead of the latest release (or the repository has
   never been released, in which case the first tag is `v1`).
2. Those commits are not all excluded — `--skip-bot-commits` drops
   bot-authored commits and `--exclude-path` drops commits that only touch
   given path prefixes, so a Dependabot bump or a README tweak alone does not
   cut a release.
3. The previous release is at least `--min-age-days` old (default 7), keeping
   releases weekly rather than firing on every merge.
4. The newest unreleased commit has settled for `--settle-hours` (default 1),
   so a release is never cut minutes after a merge.
5. The head commit's checks are green — failing or still-running checks skip
   the release unless `--allow-red-checks` is passed.

The next tag follows the `v<major>[.<minor>]` scheme already used across these
repositories: `v1` → `v1.1` → `v1.2`, with `--bump major` moving `v1.5` →
`v2.0`. `--bump auto` picks major when a commit message contains
`BREAKING CHANGE` or a `feat!:`-style marker. A tag that already exists is
never reused. Release notes come from GitHub's own
`generate_release_notes`, which produces the same "What's Changed" body as a
hand-cut release; group them by label with a `.github/release.yml` file in the
repository being released.

### Usage

```bash
# Preview the decision for a repository without creating anything.
python scripts/auto_release.py --repository charles2ke/travel --dry-run

# Cut the release for real (needs RELEASE_TOKEN or GITHUB_TOKEN).
python scripts/auto_release.py --repository charles2ke/travel
```

Options: `--min-age-days`, `--settle-hours`, `--bump {minor,major,auto}`,
`--exclude-path` (repeatable), `--skip-bot-commits`, `--allow-red-checks` and
`--dry-run`. The repository defaults to `$GITHUB_REPOSITORY`.

Every run writes a Markdown job summary naming the tag, the previous release
and the commits included — or, when nothing is released, the reason why. The
decision is also exposed as step outputs (`released`, `tag`, `previous_tag`,
`commit_count`, `reason`, `url`).

The token needs permission to create releases: `contents: write` for the
built-in `GITHUB_TOKEN`, or the `repo` scope for a personal access token.

## `rollout-auto-release.sh`

Adds the thin `Weekly release` caller workflow to every repository so they all
share the reusable workflow kept here. Each repository gets its own cron
minute, spread across Monday morning, so 20+ scheduled runs do not fire at the
same moment.

### Usage

Always start with a dry run — it prints the workflow that would be written for
each repository and makes no API calls:

```bash
./scripts/rollout-auto-release.sh --dry-run
./scripts/rollout-auto-release.sh --dry-run travel   # a single repository
```

Then roll it out for real. By default the script pushes a branch and opens a
pull request per repository; `--direct` commits straight to the default branch
instead:

```bash
./scripts/rollout-auto-release.sh
./scripts/rollout-auto-release.sh --direct travel
```

Non-dry runs need [GitHub CLI (`gh`)](https://cli.github.com/) authenticated
with the `repo` and `workflow` scopes. Override the account with `OWNER` and
the pull request branch with `BRANCH`. The repository list lives in
`REPO_ORDER` near the top of the script.

Repositories with a release backlog can be caught up immediately by running
their `Weekly release` workflow once by hand (`workflow_dispatch`, optionally
with `dry-run` first); the schedule takes over from there. For repositories
where a release should be approved by a human, point the caller's
`environment:` input at a GitHub Environment with required reviewers — the
release job then waits for approval.

## `rollout_dependabot.py`

Keeps every repository on a **weekly Sunday package upgrade**. The upgrading
itself is done by Dependabot version updates — which move each dependency to
its latest stable release and open a pull request — so this script is the
rollout that puts the same `.github/dependabot.yml` in every repository.

For each repository it reads the default branch's file list, detects the
package managers actually in use (`package.json` → npm, `*.csproj`/`*.sln` →
nuget, `requirements*.txt`/`pyproject.toml` → pip, `.github/workflows/` →
github-actions, and so on), and renders a configuration that checks every
detected ecosystem `weekly` on `sunday`. Vendored directories such as
`node_modules/` and `vendor/` are ignored, a solution directory wins over the
project directories it already covers, and each ecosystem's updates are grouped
into a single pull request. Where Dependabot supports it, `versioning-strategy:
increase` is set so the manifest itself moves up to the new version.

Repositories that already carry the rendered configuration are left untouched,
so the script is safe to re-run. The rendered file replaces any hand-written
`dependabot.yml`, in the same way `set-topics.sh` replaces a repository's topics
— change the script, not the generated file.

### Usage

Always start with a dry run — it reports what each repository would get and
writes nothing:

```bash
python scripts/rollout_dependabot.py --dry-run
python scripts/rollout_dependabot.py --dry-run travel   # a single repository
```

Then roll it out for real. By default the script pushes a branch and opens a
pull request per repository; `--direct` commits straight to the default branch:

```bash
python scripts/rollout_dependabot.py
python scripts/rollout_dependabot.py --direct travel
```

Options: `--owner` (defaults to `charles2ke`), `--branch` (the pull request
branch, `chore/weekly-dependabot` by default), `--direct`, `--dry-run`, and any
number of repository names.

The script reads a token from `ROLLOUT_TOKEN` or `GITHUB_TOKEN`. A dry run only
needs read access; writing needs a token that can push branches and open pull
requests in the target repositories (the `repo` scope). Every run prints — and,
in Actions, publishes — a Markdown summary naming each repository, what
happened and which ecosystems were detected.

The [`Weekly dependency upgrades`](../.github/workflows/dependabot-rollout.yml)
workflow runs this on a schedule so repositories created later are picked up
too. Without a `DEPENDABOT_ROLLOUT_TOKEN` secret it can only report the gaps,
because the built-in `GITHUB_TOKEN` cannot write to other repositories.

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
- `--release-drift-days` — age threshold for unreleased work (defaults to 7;
  negative values skip release drift collection).

The script reads an optional token from `ALERTS_TOKEN` or `GITHUB_TOKEN`.
Without a token it uses unauthenticated requests, which are rate limited to 60
per hour and are usually not enough to scan every repository.

It also reports **release drift**: repositories whose default branch has
unreleased commits older than `--release-drift-days` (7 by default; pass a
negative value to skip the check). The dashboard shows these in a separate
panel, so a weekly `Auto release` run that never happened is visible even when
no workflow failed.

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
