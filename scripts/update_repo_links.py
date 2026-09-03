#!/usr/bin/env python3

from __future__ import annotations

import html
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
BADGE_URL = "https://img.shields.io/badge/{label}-{message}-{color}?style=flat-square"
FIELD_COLOR = "0A66C2"
VALUE_COLOR = "2EA043"
DEFAULT_BADGES = ("Software Engineering", "Hands-on learning and experimentation")

# Repository name (case-insensitive) → (field it impacts, value it adds).
REPO_BADGES: dict[str, tuple[str, str]] = {
    "5-mins": ("Disaster Alerts", "Early warning awareness"),
    "agent-chaos-monkey": ("Reliability Engineering", "Safer agent failure recovery"),
    "baby-model": ("Private AI", "Grounded personal answers"),
    "basa": ("Elder Care", "Coordinated caregiving"),
    "design-patterns": ("Software Design", "Reusable design knowledge"),
    "graphql": ("API Engineering", "Flexible data access"),
    "message-flow": ("Software Design", "Decoupled message handling"),
    "nakshatra": ("E-Commerce", "Streamlined online shopping"),
    "night-sky": ("Astronomy Visualisation", "Accurate sky reconstruction"),
    "opentrading": ("FinTech", "Global trade execution"),
    "portfolio-watcher": ("Personal Finance", "Unified portfolio view"),
    "tax-break": ("Taxation", "Simplified tax filing"),
    "travel": ("Travel", "Easier trip discovery"),
    "workout": ("Health and Fitness", "Consistent training habits"),
}


def _badge(label: str, message: str, color: str) -> str:
    def encode(value: str) -> str:
        return urllib.parse.quote(value.replace("-", "--").replace("_", "__"), safe="")

    source = BADGE_URL.format(label=encode(label), message=encode(message), color=color)
    alt = html.escape(f"{label}: {message}", quote=True)
    return f'<img alt="{alt}" src="{source}">'


def build_badges(name: str) -> str:
    field, value = REPO_BADGES.get(name.casefold(), DEFAULT_BADGES)
    return f"{_badge('Field', field, FIELD_COLOR)} {_badge('Value', value, VALUE_COLOR)}"


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

        entries.append((name, f"[{name}]({html_url}) — {description}", build_badges(name)))

    entries.sort(key=lambda entry: entry[0].casefold())

    if not entries:
        return "1. No repositories to show yet."

    lines = []
    for position, (_, line, badges) in enumerate(entries, start=1):
        marker = f"{position}. "
        lines.append(f"{marker}{line} \n{' ' * len(marker)}{badges}")

    return "\n".join(lines)


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
