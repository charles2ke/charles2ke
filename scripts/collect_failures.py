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
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_OWNER = "charles2ke"
API_ROOT = "https://api.github.com"
REPOS_URL = API_ROOT + "/users/{owner}/repos?per_page=100&page={page}&type=owner"
RUNS_URL = API_ROOT + "/repos/{full_name}/actions/runs?per_page={per_page}&page={page}"
RELEASE_URL = API_ROOT + "/repos/{full_name}/releases/latest"
COMPARE_URL = API_ROOT + "/repos/{full_name}/compare/{base}...{head}?per_page=1"
COMMITS_URL = API_ROOT + "/repos/{full_name}/commits?per_page=1"
DEFAULT_DRIFT_DAYS = 7
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
RUN_PAGES = 2
RUNS_PER_PAGE = 100
USER_AGENT = "charles2ke-failures-dashboard"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 2.0


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub API request keeps failing after all retries."""


def _request(url: str, token: str | None) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token

    request = urllib.request.Request(url, headers=headers)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            message = f"GitHub API request failed: {error.code} {error.reason}"
            retryable = error.code in RETRY_STATUSES
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            message = f"GitHub API request failed: {reason}"
            retryable = True
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = "GitHub API request failed: invalid JSON response"
            retryable = True

        if not retryable or attempt == MAX_ATTEMPTS:
            raise GitHubAPIError(message)

        print(f"{message} (attempt {attempt}/{MAX_ATTEMPTS}), retrying...", file=sys.stderr)
        time.sleep(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))

    raise GitHubAPIError("GitHub API request failed: retries exhausted")


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


def _parse_timestamp(value: object) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def fetch_release_drift(full_name: str, token: str | None) -> dict | None:
    """Return the unreleased-work summary for a repository, or ``None``.

    "Drift" is unreleased work sitting on the default branch: either commits
    after the latest release, or a repository that has never been released.
    The dashboard uses it to surface releases that a weekly ``Auto release``
    run should have cut but didn't.
    """
    try:
        release = _request(RELEASE_URL.format(full_name=full_name), token)
    except GitHubAPIError as error:
        # A repository with no releases answers 404; that is drift, not an error.
        if "404" not in str(error):
            raise
        release = None

    latest_commit = _request(COMMITS_URL.format(full_name=full_name), token)
    head = latest_commit[0] if isinstance(latest_commit, list) and latest_commit else {}
    head_date = _parse_timestamp(((head.get("commit") or {}).get("committer") or {}).get("date"))

    if not isinstance(release, dict) or not release.get("tag_name"):
        if not head:
            return None
        return {
            "latest_tag": "",
            "release_url": "",
            "released_at": "",
            "commits_since": 1,
            "last_commit_at": head_date.strftime("%Y-%m-%dT%H:%M:%SZ") if head_date else "",
        }

    tag = str(release.get("tag_name"))
    comparison = _request(
        COMPARE_URL.format(
            full_name=full_name,
            base=urllib.parse.quote(tag, safe=""),
            head=urllib.parse.quote(str(release.get("target_commitish") or "HEAD"), safe=""),
        ),
        token,
    )
    ahead_by = int(comparison.get("ahead_by") or 0) if isinstance(comparison, dict) else 0
    if ahead_by <= 0:
        return None

    released_at = _parse_timestamp(release.get("published_at") or release.get("created_at"))
    return {
        "latest_tag": tag,
        "release_url": str(release.get("html_url") or ""),
        "released_at": released_at.strftime("%Y-%m-%dT%H:%M:%SZ") if released_at else "",
        "commits_since": ahead_by,
        "last_commit_at": head_date.strftime("%Y-%m-%dT%H:%M:%SZ") if head_date else "",
    }


def drift_age_days(drift: dict, now: dt.datetime | None = None) -> float:
    """Return how long the oldest unreleased work has been waiting, in days."""
    now = now or dt.datetime.now(dt.timezone.utc)
    reference = _parse_timestamp(drift.get("released_at")) or _parse_timestamp(
        drift.get("last_commit_at")
    )
    if reference is None:
        return 0.0
    return max((now - reference).total_seconds() / 86400, 0.0)


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


def build_snapshot(owner: str, token: str | None, drift_days: float | None = None) -> dict:
    """Build the full dashboard payload for ``owner``.

    When ``drift_days`` is set, repositories whose unreleased work is older
    than that many days are also reported under ``releases``.
    """
    repositories = []
    errors: list[str] = []
    total = 0
    releases: list[dict] = []

    for repo in fetch_repositories(owner, token):
        full_name = str(repo.get("full_name") or "")
        if not full_name:
            continue

        if drift_days is not None:
            try:
                drift = fetch_release_drift(full_name, token)
            except GitHubAPIError as error:
                message = f"{full_name} (releases): {error}"
                print(f"Skipping {message}", file=sys.stderr)
                errors.append(message)
                drift = None

            if drift is not None:
                age = drift_age_days(drift)
                if age >= drift_days:
                    releases.append(
                        {
                            "name": str(repo.get("name") or ""),
                            "full_name": full_name,
                            "url": str(repo.get("html_url") or ""),
                            "age_days": round(age, 1),
                            **drift,
                        }
                    )

        try:
            runs = fetch_runs(full_name, token)
        except GitHubAPIError as error:
            # A transient API problem for one repository must not discard the
            # whole snapshot; report it instead of aborting the build.
            message = f"{full_name}: {error}"
            print(f"Skipping {message}", file=sys.stderr)
            errors.append(message)
            continue

        failures = [summarise_failure(run) for run in unresolved_failures(runs)]
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
    releases.sort(key=lambda entry: (-entry["age_days"], entry["name"].casefold()))

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "owner": owner,
        "repository_count": len(repositories),
        "failure_count": total,
        "repositories": repositories,
        "release_drift_days": drift_days,
        "release_drift_count": len(releases),
        "releases": releases,
        "errors": errors,
        "degraded": bool(errors),
    }


def degraded_snapshot(owner: str, message: str) -> dict:
    """Return an empty snapshot flagged as incomplete because of ``message``."""
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "owner": owner,
        "repository_count": 0,
        "failure_count": 0,
        "repositories": [],
        "release_drift_days": None,
        "release_drift_count": 0,
        "releases": [],
        "errors": [message],
        "degraded": True,
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
    parser.add_argument(
        "--release-drift-days",
        type=float,
        default=DEFAULT_DRIFT_DAYS,
        help=(
            "Report repositories whose unreleased work is at least this many days "
            f"old (default: {DEFAULT_DRIFT_DAYS}; use a negative value to skip)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = os.getenv("ALERTS_TOKEN") or os.getenv("GITHUB_TOKEN")

    drift_days = args.release_drift_days if args.release_drift_days >= 0 else None

    try:
        snapshot = build_snapshot(args.owner, token, drift_days)
    except GitHubAPIError as error:
        # The dashboard is a best-effort snapshot: publish a degraded payload
        # rather than failing the whole Pages deployment on an API outage.
        print(f"{error}; publishing a degraded snapshot.", file=sys.stderr)
        snapshot = degraded_snapshot(args.owner, str(error))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")

    print(
        f"Wrote {snapshot['failure_count']} unresolved failures across "
        f"{snapshot['repository_count']} repositories to {args.output}."
    )
    if drift_days is not None:
        print(
            f"{snapshot['release_drift_count']} repositories have unreleased commits "
            f"older than {drift_days:g} day(s)."
        )
    if snapshot["errors"]:
        print(f"Snapshot is incomplete: {'; '.join(snapshot['errors'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
