# Scripts

## `set-topics.sh`

Applies a curated set of GitHub topics to every repository owned by
`charles2ke`. Repository topics are metadata (not files), so they can't be
set by committing a normal code change — this script exists to make that
one-time (or re-runnable) operation reviewable and repeatable instead of
requiring a manual visit to each repo's settings page.

### Prerequisites

- Bash 4 or later.
- [GitHub CLI (`gh`)](https://cli.github.com/) installed.
- `gh` authenticated (`gh auth login`) with a token that has at least the
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
