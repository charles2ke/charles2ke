#!/usr/bin/env python3
"""Cut a release for a repository when its default branch has unreleased work.

The script is the engine behind the reusable ``Auto release`` workflow
(``.github/workflows/auto-release.yml``). It answers one question per run:
*does this repository deserve a new release right now?* — and only tags and
publishes one when every gate agrees.

Gates, in order:

1. There is at least one commit on the default branch after the latest
   release (or the repository has never been released).
2. Those commits are not all excluded: documentation-only paths and bot
   commits can be filtered out so a Dependabot bump does not cut a release.
3. The previous release is older than ``--min-age-days`` (default 7), so
   releases stay weekly rather than firing on every merge.
4. The newest releasable commit has settled for ``--settle-hours``, so a
   release is never cut minutes after a merge.
5. The head commit's checks are green (unless ``--allow-red-checks``).

The next tag follows the ``v<major>[.<minor>]`` scheme already used across
these repositories: ``v1`` -> ``v1.1`` -> ``v1.2``, and a major bump moves
``v1.5`` -> ``v2.0``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
USER_AGENT = "charles2ke-auto-release"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 2.0

INITIAL_VERSION = "v1"
VERSION_PATTERN = re.compile(r"^v(\d+)(?:\.(\d+))?$")
BREAKING_PATTERN = re.compile(
    r"(^|\n)\s*(BREAKING[ -]CHANGE|breaking change)|^[a-z]+(\([^)]*\))?!:",
    re.IGNORECASE,
)
BOT_SUFFIX = "[bot]"
GREEN_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
RED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
MAX_COMMITS_INSPECTED = 100


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub API request keeps failing after all retries."""


class NotFoundError(GitHubAPIError):
    """Raised when the GitHub API answers 404 (no release, no tag, ...)."""


def _request(
    url: str,
    token: str | None,
    *,
    method: str = "GET",
    payload: dict | None = None,
) -> object:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token

    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            message = f"GitHub API request failed: {error.code} {error.reason} ({url})"
            if error.code == 404:
                raise NotFoundError(message) from error
            retryable = error.code in RETRY_STATUSES
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            message = f"GitHub API request failed: {reason} ({url})"
            retryable = True
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = f"GitHub API request failed: invalid JSON response ({url})"
            retryable = True

        if not retryable or attempt == MAX_ATTEMPTS:
            raise GitHubAPIError(message)

        print(f"{message} (attempt {attempt}/{MAX_ATTEMPTS}), retrying...", file=sys.stderr)
        time.sleep(RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1))

    raise GitHubAPIError("GitHub API request failed: retries exhausted")


def parse_version(tag: str) -> tuple[int, int] | None:
    """Return ``(major, minor)`` for ``v1`` / ``v1.5``-style tags, else ``None``."""
    match = VERSION_PATTERN.match(str(tag).strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def next_version(latest_tag: str | None, bump: str, existing_tags: set[str]) -> str:
    """Return the next free tag after ``latest_tag`` for the requested ``bump``."""
    if not latest_tag:
        candidate = INITIAL_VERSION
        while candidate in existing_tags:
            major, minor = parse_version(candidate) or (1, 0)
            candidate = f"v{major}.{minor + 1}"
        return candidate

    parsed = parse_version(latest_tag)
    if parsed is None:
        raise ValueError(
            f"Cannot derive the next version from {latest_tag!r}; "
            "expected a v<major>[.<minor>] tag."
        )

    major, minor = parsed
    if bump == "major":
        major, minor = major + 1, 0
    else:
        minor += 1

    candidate = f"v{major}.{minor}"
    while candidate in existing_tags:
        minor += 1
        candidate = f"v{major}.{minor}"
    return candidate


def wants_major_bump(commits: list[dict]) -> bool:
    """Return ``True`` when a commit message signals a breaking change."""
    for commit in commits:
        message = str((commit.get("commit") or {}).get("message") or "")
        for line in message.splitlines():
            if BREAKING_PATTERN.search(line):
                return True
    return False


def _parse_timestamp(value: str | None) -> dt.datetime | None:
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


def commit_timestamp(commit: dict) -> dt.datetime | None:
    details = commit.get("commit") or {}
    committer = details.get("committer") or details.get("author") or {}
    return _parse_timestamp(committer.get("date"))


def is_bot_commit(commit: dict) -> bool:
    author = commit.get("author") or {}
    login = str(author.get("login") or "")
    if author.get("type") == "Bot" or login.endswith(BOT_SUFFIX):
        return True
    name = str(((commit.get("commit") or {}).get("author") or {}).get("name") or "")
    return name.endswith(BOT_SUFFIX)


def matches_excluded_paths(files: list[dict], excluded: list[str]) -> bool:
    """Return ``True`` when every changed file sits under an excluded prefix."""
    if not excluded:
        return False

    names = [str(entry.get("filename") or "") for entry in files if entry.get("filename")]
    if not names:
        return False

    return all(
        any(name == prefix or name.startswith(prefix.rstrip("/") + "/") for prefix in excluded)
        for name in names
    )


class Repository:
    """Thin GitHub API wrapper scoped to a single repository."""

    def __init__(self, full_name: str, token: str | None):
        self.full_name = full_name
        self.token = token

    def _url(self, path: str) -> str:
        return f"{API_ROOT}/repos/{self.full_name}{path}"

    def get(self, path: str) -> object:
        return _request(self._url(path), self.token)

    def default_branch(self) -> str:
        payload = self.get("")
        branch = (payload or {}).get("default_branch") if isinstance(payload, dict) else None
        return str(branch or "main")

    def latest_release(self) -> dict | None:
        try:
            payload = self.get("/releases/latest")
        except NotFoundError:
            return None
        return payload if isinstance(payload, dict) else None

    def tags(self) -> list[dict]:
        payload = self.get("/tags?per_page=100")
        return payload if isinstance(payload, list) else []

    def compare(self, base: str, head: str) -> dict:
        base_ref = urllib.parse.quote(base, safe="")
        head_ref = urllib.parse.quote(head, safe="")
        payload = self.get(f"/compare/{base_ref}...{head_ref}?per_page=100")
        return payload if isinstance(payload, dict) else {}

    def commits(self, ref: str, per_page: int = 100) -> list[dict]:
        payload = self.get(f"/commits?sha={urllib.parse.quote(ref, safe='')}&per_page={per_page}")
        return payload if isinstance(payload, list) else []

    def commit(self, sha: str) -> dict:
        payload = self.get(f"/commits/{urllib.parse.quote(sha, safe='')}")
        return payload if isinstance(payload, dict) else {}

    def check_runs(self, ref: str) -> list[dict]:
        ref_path = urllib.parse.quote(ref, safe="")
        payload = self.get(f"/commits/{ref_path}/check-runs?per_page=100")
        runs = payload.get("check_runs") if isinstance(payload, dict) else None
        return runs if isinstance(runs, list) else []

    def combined_status(self, ref: str) -> str:
        ref_path = urllib.parse.quote(ref, safe="")
        payload = self.get(f"/commits/{ref_path}/status")
        state = payload.get("state") if isinstance(payload, dict) else None
        return str(state or "pending")

    def create_release(self, tag: str, target: str) -> dict:
        payload = _request(
            self._url("/releases"),
            self.token,
            method="POST",
            payload={
                "tag_name": tag,
                "target_commitish": target,
                "name": tag,
                "generate_release_notes": True,
                "draft": False,
                "prerelease": False,
            },
        )
        return payload if isinstance(payload, dict) else {}


def latest_release_tag(repository: Repository) -> tuple[str | None, dt.datetime | None]:
    """Return the newest release tag and its publication time, if any."""
    release = repository.latest_release()
    if release:
        tag = str(release.get("tag_name") or "") or None
        published = _parse_timestamp(release.get("published_at") or release.get("created_at"))
        if tag:
            return tag, published

    versions = []
    for tag in repository.tags():
        name = str(tag.get("name") or "")
        parsed = parse_version(name)
        if parsed is not None:
            versions.append((parsed, name))

    if not versions:
        return None, None

    versions.sort()
    return versions[-1][1], None


def check_status(repository: Repository, sha: str) -> tuple[bool, str]:
    """Return ``(green, description)`` for the checks on ``sha``."""
    red: list[str] = []
    pending: list[str] = []

    for check in repository.check_runs(sha):
        name = str(check.get("name") or "check")
        if check.get("status") != "completed":
            pending.append(name)
        elif str(check.get("conclusion") or "") in RED_CONCLUSIONS:
            red.append(name)

    if red:
        return False, "failing checks: " + ", ".join(sorted(red))

    state = repository.combined_status(sha)
    if state == "failure":
        return False, "failing commit status"

    if pending:
        return False, "checks still running: " + ", ".join(sorted(pending))

    return True, "checks are green"


def releasable_commits(
    repository: Repository,
    commits: list[dict],
    *,
    exclude_paths: list[str],
    skip_bot_commits: bool,
) -> list[dict]:
    """Drop bot commits and commits that only touch excluded paths."""
    kept: list[dict] = []

    for commit in commits:
        if skip_bot_commits and is_bot_commit(commit):
            continue

        if exclude_paths:
            sha = str(commit.get("sha") or "")
            details = repository.commit(sha) if sha else {}
            files = details.get("files") or []
            if isinstance(files, list) and matches_excluded_paths(files, exclude_paths):
                continue

        kept.append(commit)

    return kept


class Decision:
    """Outcome of one auto-release evaluation."""

    def __init__(
        self,
        *,
        released: bool,
        reason: str,
        tag: str | None = None,
        previous_tag: str | None = None,
        commit_count: int = 0,
        commits: list[dict] | None = None,
        url: str = "",
        dry_run: bool = False,
    ):
        self.released = released
        self.reason = reason
        self.tag = tag
        self.previous_tag = previous_tag
        self.commit_count = commit_count
        self.commits = commits or []
        self.url = url
        self.dry_run = dry_run


def evaluate(
    repository: Repository,
    *,
    min_age_days: float,
    settle_hours: float,
    bump: str,
    exclude_paths: list[str],
    skip_bot_commits: bool,
    require_green_checks: bool,
    dry_run: bool,
    now: dt.datetime | None = None,
) -> Decision:
    """Decide whether ``repository`` needs a release, and cut one if so."""
    now = now or dt.datetime.now(dt.timezone.utc)
    branch = repository.default_branch()
    previous_tag, released_at = latest_release_tag(repository)

    if previous_tag:
        comparison = repository.compare(previous_tag, branch)
        commits = comparison.get("commits") or []
        ahead_by = int(comparison.get("ahead_by") or len(commits))
    else:
        commits = list(reversed(repository.commits(branch, per_page=MAX_COMMITS_INSPECTED)))
        ahead_by = len(commits)

    if ahead_by == 0 or not commits:
        return Decision(
            released=False,
            reason=f"nothing to release: {branch} matches {previous_tag or 'the initial commit'}",
            previous_tag=previous_tag,
            dry_run=dry_run,
        )

    candidates = releasable_commits(
        repository,
        commits,
        exclude_paths=exclude_paths,
        skip_bot_commits=skip_bot_commits,
    )
    if not candidates:
        return Decision(
            released=False,
            reason=(
                f"nothing to release: all {ahead_by} unreleased commit(s) are excluded "
                "(bot commits or excluded paths)"
            ),
            previous_tag=previous_tag,
            commit_count=ahead_by,
            dry_run=dry_run,
        )

    if released_at is not None:
        release_age_days = (now - released_at).total_seconds() / 86400
        if release_age_days < min_age_days:
            return Decision(
                released=False,
                reason=(
                    f"too soon: {previous_tag} is {release_age_days:.1f} day(s) old, "
                    f"minimum is {min_age_days:g}"
                ),
                previous_tag=previous_tag,
                commit_count=len(candidates),
                commits=candidates,
                dry_run=dry_run,
            )

    newest = max(
        (stamp for stamp in (commit_timestamp(commit) for commit in candidates) if stamp),
        default=None,
    )
    if newest is not None:
        settled_hours = (now - newest).total_seconds() / 3600
        if settled_hours < settle_hours:
            return Decision(
                released=False,
                reason=(
                    f"not settled: newest commit is {settled_hours:.1f} hour(s) old, "
                    f"minimum is {settle_hours:g}"
                ),
                previous_tag=previous_tag,
                commit_count=len(candidates),
                commits=candidates,
                dry_run=dry_run,
            )

    head_sha = str(candidates[-1].get("sha") or commits[-1].get("sha") or branch)
    if require_green_checks:
        green, description = check_status(repository, head_sha)
        if not green:
            return Decision(
                released=False,
                reason=f"checks are not green: {description}",
                previous_tag=previous_tag,
                commit_count=len(candidates),
                commits=candidates,
                dry_run=dry_run,
            )

    existing_tags = {str(tag.get("name") or "") for tag in repository.tags()}
    effective_bump = bump
    if bump == "auto":
        effective_bump = "major" if wants_major_bump(candidates) else "minor"

    tag = next_version(previous_tag, effective_bump, existing_tags)

    if dry_run:
        return Decision(
            released=False,
            reason=f"dry run: would release {tag} with {len(candidates)} commit(s)",
            tag=tag,
            previous_tag=previous_tag,
            commit_count=len(candidates),
            commits=candidates,
            dry_run=True,
        )

    release = repository.create_release(tag, branch)
    return Decision(
        released=True,
        reason=f"released {tag} with {len(candidates)} commit(s)",
        tag=tag,
        previous_tag=previous_tag,
        commit_count=len(candidates),
        commits=candidates,
        url=str(release.get("html_url") or ""),
    )


def render_summary(full_name: str, decision: Decision) -> str:
    """Render the decision as GitHub-flavoured Markdown for the job summary."""
    if decision.released:
        headline = f"Released **{decision.tag}**"
    elif decision.dry_run and decision.tag:
        headline = f"Dry run: would release **{decision.tag}**"
    else:
        headline = "No release cut"

    lines = [
        f"## Auto release — {full_name}",
        "",
        f"{headline} — {decision.reason}.",
        "",
        f"- Previous release: {decision.previous_tag or '_none_'}",
        f"- Unreleased commits: {decision.commit_count}",
    ]
    if decision.url:
        lines.append(f"- Release: {decision.url}")

    if decision.commits:
        lines.extend(["", "<details><summary>Commits</summary>", ""])
        for commit in reversed(decision.commits[-20:]):
            sha = str(commit.get("sha") or "")[:7]
            message = str((commit.get("commit") or {}).get("message") or "").splitlines()
            lines.append(f"- `{sha}` {message[0] if message else ''}")
        lines.extend(["", "</details>"])

    return "\n".join(lines) + "\n"


def write_outputs(decision: Decision) -> None:
    """Expose the decision to later workflow steps via ``GITHUB_OUTPUT``."""
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return

    values = {
        "released": "true" if decision.released else "false",
        "tag": decision.tag or "",
        "previous_tag": decision.previous_tag or "",
        "commit_count": str(decision.commit_count),
        "reason": decision.reason,
        "url": decision.url,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in values.items())


def write_summary(full_name: str, decision: Decision) -> None:
    summary = render_summary(full_name, decision)
    print(summary)

    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(summary)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        default=os.getenv("GITHUB_REPOSITORY", ""),
        help="Repository to release, as owner/repo (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--min-age-days",
        type=float,
        default=7.0,
        help="Minimum age of the previous release before a new one is cut (default: 7)",
    )
    parser.add_argument(
        "--settle-hours",
        type=float,
        default=1.0,
        help="Minimum age of the newest unreleased commit (default: 1)",
    )
    parser.add_argument(
        "--bump",
        choices=("minor", "major", "auto"),
        default="minor",
        help="Version bump to apply; 'auto' promotes to major on breaking changes",
    )
    parser.add_argument(
        "--exclude-path",
        action="append",
        default=[],
        dest="exclude_paths",
        metavar="PATH",
        help="Path prefix whose commits alone never trigger a release (repeatable)",
    )
    parser.add_argument(
        "--skip-bot-commits",
        action="store_true",
        help="Ignore commits authored by bots when deciding whether to release",
    )
    parser.add_argument(
        "--allow-red-checks",
        action="store_true",
        help="Release even when the head commit's checks are failing or pending",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the decision without creating a release",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.repository:
        print("No repository given: pass --repository owner/repo.", file=sys.stderr)
        return 2

    token = os.getenv("RELEASE_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token and not args.dry_run:
        print("No token found: set RELEASE_TOKEN or GITHUB_TOKEN.", file=sys.stderr)
        return 2

    repository = Repository(args.repository, token)
    try:
        decision = evaluate(
            repository,
            min_age_days=args.min_age_days,
            settle_hours=args.settle_hours,
            bump=args.bump,
            exclude_paths=[path for path in args.exclude_paths if path],
            skip_bot_commits=args.skip_bot_commits,
            require_green_checks=not args.allow_red_checks,
            dry_run=args.dry_run,
        )
    except (GitHubAPIError, ValueError) as error:
        print(f"Auto release failed: {error}", file=sys.stderr)
        return 1

    write_summary(args.repository, decision)
    write_outputs(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
