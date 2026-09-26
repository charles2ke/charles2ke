#!/usr/bin/env python3
"""Keep every repository on a weekly Sunday package upgrade schedule.

The upgrading itself is done by Dependabot version updates, which move each
dependency to its latest stable release and open a pull request. Dependabot is
configured per repository, so this script is the rollout: for every repository
owned by the account it

1. reads the default branch's file list and detects which package managers the
   repository actually uses (``package.json`` -> npm, ``*.csproj`` -> nuget,
   ``.github/workflows/`` -> github-actions, ...);
2. renders a ``.github/dependabot.yml`` that checks every detected ecosystem
   weekly on Sunday; and
3. writes it back — as a pull request by default, or straight to the default
   branch with ``--direct``.

Repositories that already carry the rendered configuration are left untouched,
so the script is safe to re-run. ``--dry-run`` reports the plan without writing
anything, which is how the scheduled workflow audits coverage when no rollout
token is available.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import posixpath
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_ROOT = "https://api.github.com"
USER_AGENT = "charles2ke-dependabot-rollout"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 2.0

DEFAULT_OWNER = "charles2ke"
CONFIG_PATH = ".github/dependabot.yml"
DEFAULT_BRANCH_NAME = "chore/weekly-dependabot"
COMMIT_MESSAGE = "Upgrade packages weekly with Dependabot"
PULL_REQUEST_TITLE = "Upgrade packages weekly with Dependabot"
PULL_REQUEST_BODY = (
    "Adds the weekly Dependabot configuration managed centrally by "
    "`scripts/rollout_dependabot.py`.\n\n"
    "Every Sunday Dependabot checks the package managers detected in this "
    "repository ({ecosystems}) and opens a pull request moving each dependency "
    "to its latest stable release."
)

# Dependabot runs weekly updates on this day, at this time, in this timezone.
SCHEDULE_DAY = "sunday"
SCHEDULE_TIME = "07:00"
SCHEDULE_TIMEZONE = "Etc/UTC"
GROUP_NAME = "all-dependencies"

MANAGED_HEADER = """\
# Weekly package upgrades, managed centrally by scripts/rollout_dependabot.py.
# Every Sunday Dependabot opens a pull request moving each dependency below to
# its latest stable release.
#
# Re-run the rollout script rather than editing this file by hand: the next
# rollout replaces it with the configuration rendered from the script."""

# Manifest file name -> Dependabot package ecosystem.
MANIFEST_FILES: dict[str, str] = {
    "Cargo.toml": "cargo",
    "Directory.Packages.props": "nuget",
    "Gemfile": "bundler",
    "Package.swift": "swift",
    "Pipfile": "pip",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "composer.json": "composer",
    "elm.json": "elm",
    "go.mod": "gomod",
    "mix.exs": "mix",
    "package.json": "npm",
    "packages.config": "nuget",
    "pom.xml": "maven",
    "pubspec.yaml": "pub",
    "pyproject.toml": "pip",
    "setup.cfg": "pip",
    "setup.py": "pip",
}

# Manifest file suffix -> Dependabot package ecosystem.
MANIFEST_SUFFIXES: dict[str, str] = {
    ".csproj": "nuget",
    ".fsproj": "nuget",
    ".sln": "nuget",
    ".tf": "terraform",
    ".vbproj": "nuget",
}

REQUIREMENTS_PATTERN = re.compile(r"requirements.*\.txt")
WORKFLOW_DIRECTORY = ".github/workflows/"
WORKFLOW_SUFFIXES = (".yml", ".yaml")
ACTION_FILES = frozenset({"action.yml", "action.yaml"})
GITHUB_ACTIONS = "github-actions"

# Generated code and vendored dependencies are never updated by Dependabot.
SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".terraform",
        ".venv",
        "__pycache__",
        "bower_components",
        "node_modules",
        "site-packages",
        "third_party",
        "vendor",
        "venv",
    }
)

# Ecosystems where Dependabot can raise the version requirement itself, rather
# than only widening it, so a manifest really does end up on the latest stable
# release. Other ecosystems reject the option.
VERSIONING_STRATEGY_ECOSYSTEMS = frozenset(
    {"bundler", "cargo", "composer", "helm", "mix", "npm", "pip", "pub", "uv"}
)


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub API request keeps failing after all retries."""


class NotFoundError(GitHubAPIError):
    """Raised when the GitHub API answers 404 (no config file, no branch, ...)."""


class BranchNotRolloutOwnedError(GitHubAPIError):
    """Raised when the rollout branch exists but was not created by this script."""


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


def _directory_of(path: str) -> str:
    """Return the Dependabot directory holding ``path`` (``/`` for the root)."""
    parent = posixpath.dirname(path)
    return f"/{parent}" if parent else "/"


def _is_within(directory: str, ancestor: str) -> bool:
    """Return whether ``directory`` is ``ancestor`` or nested beneath it."""
    if ancestor == "/":
        return True
    return directory == ancestor or directory.startswith(ancestor.rstrip("/") + "/")


def ecosystem_for(file_name: str) -> str | None:
    """Return the Dependabot ecosystem a manifest file name belongs to."""
    if file_name in MANIFEST_FILES:
        return MANIFEST_FILES[file_name]
    if file_name == "Dockerfile" or file_name.startswith("Dockerfile."):
        return "docker"
    if REQUIREMENTS_PATTERN.fullmatch(file_name):
        return "pip"
    for suffix, ecosystem in MANIFEST_SUFFIXES.items():
        if file_name.endswith(suffix):
            return ecosystem
    return None


def _ecosystem_order(ecosystem: str) -> tuple[int, str]:
    # Workflows are updated in every repository, so they lead the file.
    return (0 if ecosystem == GITHUB_ACTIONS else 1, ecosystem)


def detect_ecosystems(paths: list[str]) -> dict[str, list[str]]:
    """Map each package ecosystem in ``paths`` to the directories that use it."""
    found: dict[str, set[str]] = {}
    solutions: set[str] = set()
    projects: set[str] = set()

    for raw_path in paths:
        path = str(raw_path).strip().lstrip("/")
        if not path:
            continue

        segments = path.split("/")
        if any(segment in SKIPPED_DIRECTORIES for segment in segments[:-1]):
            continue

        file_name = segments[-1]
        # GitHub Actions is always configured at the repository root: Dependabot
        # scans .github/workflows/ and the root action file from there.
        if path.startswith(WORKFLOW_DIRECTORY) and file_name.endswith(WORKFLOW_SUFFIXES):
            found.setdefault(GITHUB_ACTIONS, set()).add("/")
            continue
        if len(segments) == 1 and file_name in ACTION_FILES:
            found.setdefault(GITHUB_ACTIONS, set()).add("/")
            continue

        ecosystem = ecosystem_for(file_name)
        if ecosystem is None:
            continue

        directory = _directory_of(path)
        if ecosystem == "nuget":
            (solutions if file_name.endswith(".sln") else projects).add(directory)
            continue

        found.setdefault(ecosystem, set()).add(directory)

    # A solution already pulls in the projects it references, so prefer solution
    # directories and only fall back to project directories it doesn't cover.
    uncovered_projects = {
        directory
        for directory in projects
        if not any(_is_within(directory, solution) for solution in solutions)
    }
    nuget_directories = solutions | uncovered_projects
    if nuget_directories:
        found["nuget"] = nuget_directories

    return {
        ecosystem: sorted(directories)
        for ecosystem, directories in sorted(found.items(), key=lambda item: _ecosystem_order(item[0]))
    }


def render_config(ecosystems: dict[str, list[str]]) -> str:
    """Render the ``.github/dependabot.yml`` for the detected ``ecosystems``."""
    lines = [MANAGED_HEADER, "", "version: 2", "updates:"]

    for ecosystem, directories in ecosystems.items():
        lines.append(f"  - package-ecosystem: {ecosystem}")
        if len(directories) == 1:
            lines.append(f'    directory: "{directories[0]}"')
        else:
            lines.append("    directories:")
            lines.extend(f'      - "{directory}"' for directory in directories)
        lines.extend(
            [
                "    schedule:",
                "      interval: weekly",
                f"      day: {SCHEDULE_DAY}",
                f'      time: "{SCHEDULE_TIME}"',
                f"      timezone: {SCHEDULE_TIMEZONE}",
            ]
        )
        if ecosystem in VERSIONING_STRATEGY_ECOSYSTEMS:
            lines.append("    versioning-strategy: increase")
        lines.extend(
            [
                "    groups:",
                f"      {GROUP_NAME}:",
                "        patterns:",
                '          - "*"',
            ]
        )

    return "\n".join(lines) + "\n"


def _repository_urls(token: str | None) -> list[str]:
    public = API_ROOT + "/users/{owner}/repos?per_page=100&page={page}&type=owner"
    if not token:
        return [public]
    # The authenticated list also covers private repositories, but only a
    # user-to-server token can read it, so the public list stays as a fallback.
    return [API_ROOT + "/user/repos?per_page=100&page={page}&affiliation=owner", public]


def _fetch_owned_repositories(url_template: str, owner: str, token: str | None) -> list[dict]:
    repositories: list[dict] = []
    seen: set[str] = set()
    page = 1

    while True:
        url = url_template.format(owner=urllib.parse.quote(owner), page=page)
        batch = _request(url, token)
        if not isinstance(batch, list) or not batch:
            break

        for repository in batch:
            full_name = str(repository.get("full_name", ""))
            login = str((repository.get("owner") or {}).get("login", ""))
            if login.casefold() != owner.casefold():
                continue
            if repository.get("fork") or repository.get("archived"):
                continue
            if full_name in seen:
                continue
            seen.add(full_name)
            repositories.append(repository)

        page += 1

    repositories.sort(key=lambda repository: str(repository.get("name", "")).casefold())
    return repositories


def fetch_repositories(owner: str, token: str | None) -> list[dict]:
    """Return every non-fork, non-archived repository owned by ``owner``."""
    urls = _repository_urls(token)

    for index, url_template in enumerate(urls):
        last = index + 1 == len(urls)
        try:
            repositories = _fetch_owned_repositories(url_template, owner, token)
        except GitHubAPIError as error:
            if last:
                raise
            print(f"Falling back to the public repository list: {error}", file=sys.stderr)
            continue

        if repositories or last:
            return repositories

    return []


def fetch_paths(full_name: str, ref: str, token: str | None) -> tuple[list[str], bool]:
    """Return the file paths on ``ref``, plus whether the listing was truncated."""
    url = (
        f"{API_ROOT}/repos/{full_name}/git/trees/"
        f"{urllib.parse.quote(ref, safe='')}?recursive=1"
    )
    try:
        payload = _request(url, token)
    except NotFoundError:
        return [], False

    if not isinstance(payload, dict):
        return [], False

    entries = payload.get("tree") or []
    paths = [
        str(entry.get("path", ""))
        for entry in entries
        if isinstance(entry, dict) and entry.get("type") == "blob"
    ]
    return paths, bool(payload.get("truncated"))


def fetch_config(full_name: str, ref: str, token: str | None) -> tuple[str | None, str | None]:
    """Return the current ``.github/dependabot.yml`` text and blob sha."""
    url = (
        f"{API_ROOT}/repos/{full_name}/contents/{CONFIG_PATH}"
        f"?ref={urllib.parse.quote(ref, safe='')}"
    )
    try:
        payload = _request(url, token)
    except NotFoundError:
        return None, None

    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None, str(payload.get("sha")) if isinstance(payload, dict) else None

    content = base64.b64decode(str(payload.get("content", "")))
    return content.decode("utf-8", errors="replace"), str(payload.get("sha"))


def branch_head(full_name: str, branch: str, token: str | None) -> str | None:
    url = f"{API_ROOT}/repos/{full_name}/git/ref/heads/{urllib.parse.quote(branch, safe='')}"
    try:
        payload = _request(url, token)
    except NotFoundError:
        return None
    if not isinstance(payload, dict):
        return None
    return str((payload.get("object") or {}).get("sha") or "") or None


def commit_message(full_name: str, sha: str, token: str | None) -> str:
    """Return the message of the commit at ``sha``."""
    url = f"{API_ROOT}/repos/{full_name}/git/commits/{urllib.parse.quote(sha, safe='')}"
    payload = _request(url, token)
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("message") or "")


def ensure_branch(full_name: str, branch: str, base_sha: str, token: str | None) -> None:
    """Point ``branch`` at ``base_sha``, creating it when it does not exist."""
    head = branch_head(full_name, branch, token)
    if head is None:
        _request(
            f"{API_ROOT}/repos/{full_name}/git/refs",
            token,
            method="POST",
            payload={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        return

    if head == base_sha:
        return

    # The branch already exists and points elsewhere. Only reset it when its
    # tip is a commit this script made previously; otherwise it may carry
    # unrelated work that force-updating the ref would discard.
    if not commit_message(full_name, head, token).startswith(COMMIT_MESSAGE):
        raise BranchNotRolloutOwnedError(
            f"refs/heads/{branch} in {full_name} already exists and its tip "
            f"({head}) was not created by this script; refusing to force-update it"
        )

    _request(
        f"{API_ROOT}/repos/{full_name}/git/refs/heads/{urllib.parse.quote(branch, safe='')}",
        token,
        method="PATCH",
        payload={"sha": base_sha, "force": True},
    )


def write_config(
    full_name: str,
    branch: str,
    content: str,
    sha: str | None,
    token: str | None,
) -> None:
    payload: dict[str, object] = {
        "message": COMMIT_MESSAGE,
        "branch": branch,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha

    _request(
        f"{API_ROOT}/repos/{full_name}/contents/{CONFIG_PATH}",
        token,
        method="PUT",
        payload=payload,
    )


def open_pull_request(
    full_name: str,
    branch: str,
    base: str,
    ecosystems: list[str],
    token: str | None,
) -> str:
    """Return the URL of the rollout pull request, opening one when needed."""
    head = f"{full_name.split('/')[0]}:{branch}"
    existing = _request(
        f"{API_ROOT}/repos/{full_name}/pulls"
        f"?state=open&head={urllib.parse.quote(head, safe=':')}",
        token,
    )
    if isinstance(existing, list) and existing:
        return str(existing[0].get("html_url", ""))

    created = _request(
        f"{API_ROOT}/repos/{full_name}/pulls",
        token,
        method="POST",
        payload={
            "title": PULL_REQUEST_TITLE,
            "head": branch,
            "base": base,
            "body": PULL_REQUEST_BODY.format(
                ecosystems=", ".join(ecosystems) or "none",
            ),
        },
    )
    return str(created.get("html_url", "")) if isinstance(created, dict) else ""


@dataclass
class Outcome:
    """What the rollout did — or would do — for one repository."""

    repository: str
    status: str
    detail: str = ""
    url: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def changed(self) -> bool:
        return self.status in {"created", "updated"}


def roll_out_repository(
    repository: dict,
    *,
    token: str | None,
    branch: str,
    direct: bool,
    dry_run: bool,
) -> Outcome:
    """Bring one repository onto the weekly Sunday Dependabot schedule."""
    full_name = str(repository.get("full_name", ""))
    default_branch = str(repository.get("default_branch") or "main")

    paths, truncated = fetch_paths(full_name, default_branch, token)
    if truncated:
        return Outcome(
            full_name,
            "failed",
            "file listing truncated; refusing to render from a partial manifest set",
        )

    ecosystems = detect_ecosystems(paths)
    if not ecosystems:
        return Outcome(full_name, "skipped", "no package manifests found")

    desired = render_config(ecosystems)
    summary = ", ".join(ecosystems)

    current, sha = fetch_config(full_name, default_branch, token)
    if current == desired:
        return Outcome(full_name, "up to date", summary)

    if dry_run:
        status = "would update" if current is not None else "would create"
        return Outcome(full_name, status, summary)

    target_branch = default_branch
    if not direct:
        base_sha = branch_head(full_name, default_branch, token)
        if base_sha is None:
            return Outcome(full_name, "failed", f"no {default_branch} branch to branch from")
        target_branch = branch
        ensure_branch(full_name, target_branch, base_sha, token)
        # The working branch now matches the default branch, so the blob sha
        # read above is the one the contents API expects.

    write_config(full_name, target_branch, desired, sha, token)

    url = ""
    if not direct:
        url = open_pull_request(
            full_name,
            target_branch,
            default_branch,
            list(ecosystems),
            token,
        )

    return Outcome(full_name, "updated" if current is not None else "created", summary, url)


def render_summary(outcomes: list[Outcome], *, dry_run: bool) -> str:
    """Render the Markdown job summary for a rollout."""
    heading = "# Weekly Dependabot rollout\n\n"
    if not outcomes:
        return heading + "No repositories to roll out.\n"

    mode = "Planned changes only (dry run)." if dry_run else "Applied changes."
    lines = [heading, f"{mode}\n\n", "| Repository | Status | Details |\n", "| --- | --- | --- |\n"]
    for outcome in outcomes:
        detail = outcome.detail
        if outcome.url:
            detail = f"{detail} — [pull request]({outcome.url})" if detail else f"[pull request]({outcome.url})"
        lines.append(f"| {outcome.repository} | {outcome.status} | {detail or '—'} |\n")

    return "".join(lines)


def write_summary(summary: str) -> None:
    print(summary)

    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(summary)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repositories",
        nargs="*",
        metavar="REPOSITORY",
        help="Repositories to roll out, as 'name' or 'owner/name' (default: all of them)",
    )
    parser.add_argument(
        "--owner",
        default=os.getenv("OWNER", DEFAULT_OWNER),
        help=f"GitHub account whose repositories are updated (default: {DEFAULT_OWNER})",
    )
    parser.add_argument(
        "--branch",
        default=os.getenv("BRANCH", DEFAULT_BRANCH_NAME),
        help=f"Branch used for the rollout pull requests (default: {DEFAULT_BRANCH_NAME})",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Commit straight to the default branch instead of opening a pull request",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the plan without writing any configuration",
    )
    return parser.parse_args(argv)


def select_repositories(repositories: list[dict], wanted: list[str], owner: str) -> list[dict]:
    """Keep only the repositories named on the command line, in that order."""
    if not wanted:
        return repositories

    by_name = {str(repository.get("name", "")).casefold(): repository for repository in repositories}
    selected: list[dict] = []
    for name in wanted:
        owner_part, sep, short_name = name.rpartition("/")
        if sep and owner_part.casefold() != owner.casefold():
            raise ValueError(f"repository '{name}' does not belong to owner '{owner}'")
        repository = by_name.get(short_name.casefold())
        if repository is None:
            raise ValueError(f"unknown repository '{name}' for owner '{owner}'")
        selected.append(repository)
    return selected


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    token = os.getenv("ROLLOUT_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token and not args.dry_run:
        print("No token found: set ROLLOUT_TOKEN or GITHUB_TOKEN.", file=sys.stderr)
        return 2

    try:
        repositories = select_repositories(
            fetch_repositories(args.owner, token), args.repositories, args.owner
        )
    except (GitHubAPIError, ValueError) as error:
        print(f"Dependabot rollout failed: {error}", file=sys.stderr)
        return 1

    outcomes: list[Outcome] = []
    for repository in repositories:
        full_name = str(repository.get("full_name", ""))
        try:
            outcomes.append(
                roll_out_repository(
                    repository,
                    token=token,
                    branch=args.branch,
                    direct=args.direct,
                    dry_run=args.dry_run,
                )
            )
        except GitHubAPIError as error:
            outcomes.append(Outcome(full_name, "failed", str(error)))

    write_summary(render_summary(outcomes, dry_run=args.dry_run))

    pending = [outcome for outcome in outcomes if outcome.status.startswith("would ")]
    if pending and os.getenv("GITHUB_ACTIONS"):
        print(
            f"::warning::{len(pending)} repositories still need the weekly Dependabot configuration.",
        )

    failures = [outcome for outcome in outcomes if outcome.failed]
    if failures:
        print(f"Completed with {len(failures)} failure(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
