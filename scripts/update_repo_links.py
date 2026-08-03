#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_OWNER = "charles2ke"
PROFILE_REPO = f"{REPO_OWNER}/{REPO_OWNER}"
README_PATH = Path(__file__).resolve().parents[1] / "README.md"
SECTION_START = "<!-- repo-links:start -->"
SECTION_END = "<!-- repo-links:end -->"
API_URL = "https://api.github.com/users/{owner}/repos?per_page=100&page={page}&type=owner&sort=full_name"


def fetch_repositories(owner: str) -> list[dict[str, object]]:
    token = os.getenv("GITHUB_TOKEN")
    repos: list[dict[str, object]] = []
    page = 1

    while True:
        request = urllib.request.Request(
            API_URL.format(owner=urllib.parse.quote(owner), page=page),
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"{owner}-profile-readme-updater",
                **({"Authorization": "Bearer " + token} if token else {}),
            },
        )

        try:
            with urllib.request.urlopen(request) as response:
                batch = json.load(response)
        except urllib.error.HTTPError as error:
            raise SystemExit(f"GitHub API request failed: {error.code} {error.reason}") from error

        if not batch:
            break

        repos.extend(batch)
        page += 1

    return repos


def build_repo_lines(repositories: list[dict[str, object]]) -> str:
    entries = []

    for repo in repositories:
        full_name = str(repo.get("full_name", ""))
        if full_name == PROFILE_REPO or repo.get("fork"):
            continue

        name = str(repo.get("name", "")).strip()
        html_url = str(repo.get("html_url", "")).strip()
        description = " ".join(str(repo.get("description") or "No description provided.").split())

        entries.append(f"- [{name}]({html_url}) — {description}")

    if not entries:
        entries.append("- No repositories to show yet.")

    return "\n".join(entries)


def update_readme(section_body: str) -> bool:
    contents = README_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"{re.escape(SECTION_START)}\n.*?\n{re.escape(SECTION_END)}",
        re.DOTALL,
    )
    replacement = f"{SECTION_START}\n{section_body}\n{SECTION_END}"
    updated = pattern.sub(replacement, contents, count=1)

    if updated == contents:
        return False

    README_PATH.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    if not README_PATH.exists():
        raise SystemExit(f"README not found at {README_PATH}")

    repositories = fetch_repositories(REPO_OWNER)
    changed = update_readme(build_repo_lines(repositories))
    print("README updated." if changed else "README already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
