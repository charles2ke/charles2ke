#!/usr/bin/env python3
"""Collect unresolved GitHub Actions failures across all public repositories.

The output is a JSON snapshot consumed by the static failures dashboard
published to GitHub Pages (see ``site/failures.html``).

A failure is considered *unresolved* when the most recent run of a given
workflow on a given branch failed. A later successful run of the same
workflow on the same branch resolves the earlier failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_OWNER = "charles2ke"
API_ROOT = "https://api.github.com"
REPOS_URL = API_ROOT + "/users/{owner}/repos?per_page=100&page={page}&type=owner"
RUNS_URL = API_ROOT + "/repos/{full_name}/actions/runs?per_page={per_page}&page={page}"
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
RUN_PAGES = 2
RUNS_PER_PAGE = 100
USER_AGENT = "charles2ke-failures-dashboard"


def _request(url: str, token: str | None) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"GitHub API request failed: {error.code} {error.reason}") from error
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise SystemExit(f"GitHub API request failed: {reason}") from error
    except json.JSONDecodeError as error:
        raise SystemExit("GitHub API request failed: invalid JSON response") from error


def fetch_repositories(owner: str, token: str | None) -> list[dict]:
    """Return every public, non-fork, non-archived repository for ``owner``."""
    repositories: list[dict] = []
    page = 1

    while True:
        url = REPOS_URL.format(owner=urllib.parse.quote(owner), page=page)
        batch = _request(url, token)
        if not isinstance(batch, list) or not batch:
            break

        for repo in batch:
            if repo.get("private") or repo.get("fork") or repo.get("archived"):
                continue
            repositories.append(repo)

        page += 1

    repositories.sort(key=lambda repo: str(repo.get("name", "")).casefold())
    return repositories


def fetch_runs(full_name: str, token: str | None) -> list[dict]:
    """Return recent workflow runs for a repository, newest first."""
    runs: list[dict] = []

    for page in range(1, RUN_PAGES + 1):
        url = RUNS_URL.format(
            full_name=full_name,
            per_page=RUNS_PER_PAGE,
            page=page,
        )
        payload = _request(url, token)
        batch = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
        if not batch:
            break

        runs.extend(batch)
        if len(batch) < RUNS_PER_PAGE:
            break

    return runs


def _run_sort_key(run: dict) -> tuple[str, int]:
    timestamp = str(run.get("updated_at") or run.get("created_at") or "")
    return (timestamp, int(run.get("id") or 0))


def unresolved_failures(runs: list[dict]) -> list[dict]:
    """Reduce ``runs`` to the still-failing latest run per workflow and branch."""
    latest: dict[tuple[object, str], dict] = {}

    for run in runs:
        if run.get("status") != "completed":
            continue

        key = (run.get("workflow_id"), str(run.get("head_branch") or ""))
        current = latest.get(key)
        if current is None or _run_sort_key(run) > _run_sort_key(current):
            latest[key] = run

    failures = [
        run for run in latest.values() if run.get("conclusion") in FAILED_CONCLUSIONS
    ]
    failures.sort(key=_run_sort_key, reverse=True)
    return failures


def summarise_failure(run: dict) -> dict:
    """Project a workflow run onto the fields the dashboard needs."""
    actor = run.get("triggering_actor") or run.get("actor") or {}
    return {
        "id": run.get("id"),
        "workflow": run.get("name") or "Workflow",
        "run_number": run.get("run_number"),
        "branch": run.get("head_branch") or "",
        "event": run.get("event") or "",
        "conclusion": run.get("conclusion") or "failure",
        "url": run.get("html_url") or "",
        "updated_at": run.get("updated_at") or run.get("created_at") or "",
        "actor": (actor or {}).get("login") or "",
        "title": run.get("display_title") or "",
    }


def build_snapshot(owner: str, token: str | None) -> dict:
    """Build the full dashboard payload for ``owner``."""
    repositories = []
    total = 0

    for repo in fetch_repositories(owner, token):
        full_name = str(repo.get("full_name") or "")
        if not full_name:
            continue

        failures = [
            summarise_failure(run) for run in unresolved_failures(fetch_runs(full_name, token))
        ]
        if not failures:
            continue

        total += len(failures)
        repositories.append(
            {
                "name": str(repo.get("name") or ""),
                "full_name": full_name,
                "url": str(repo.get("html_url") or ""),
                "description": str(repo.get("description") or ""),
                "failures": failures,
            }
        )

    repositories.sort(key=lambda entry: (-len(entry["failures"]), entry["name"].casefold()))

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "owner": owner,
        "repository_count": len(repositories),
        "failure_count": total,
        "repositories": repositories,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner",
        default=REPO_OWNER,
        help=f"GitHub account to scan (default: {REPO_OWNER})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("_site/failures.json"),
        help="Path of the JSON snapshot to write (default: _site/failures.json)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.getenv("ALERTS_TOKEN") or os.getenv("GITHUB_TOKEN")

    snapshot = build_snapshot(args.owner, token)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")

    print(
        f"Wrote {snapshot['failure_count']} unresolved failures across "
        f"{snapshot['repository_count']} repositories to {args.output}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
