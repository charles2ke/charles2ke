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
NO_DESCRIPTION = "No description provided."
SUMMARY_PREFIX = "🧠"
SUMMARY_FALLBACK = "A personal project — see the repository for details."
SUMMARY_MAX_LENGTH = 200

# Repository name (case-insensitive) → (field it impacts, value it adds).
REPO_BADGES: dict[str, tuple[str, str]] = {
    "5-mins": ("Disaster Alerts", "Early warning awareness"),
    "advantage": ("Insurance", "One-stop policy management"),
    "aero": ("Aerospace Engineering", "Applied flight fundamentals"),
    "agent-chaos-monkey": ("Reliability Engineering", "Safer agent failure recovery"),
    "baby-model": ("Private AI", "Grounded personal answers"),
    "basa": ("Elder Care", "Coordinated caregiving"),
    "crabs": ("Security", "Safer open agent access"),
    "design-patterns": ("Software Design", "Reusable design knowledge"),
    "gitdb": ("Data Storage", "Git-backed persistence"),
    "graphql": ("API Engineering", "Flexible data access"),
    "jarvis": ("Personal AI", "Always-on personal companion"),
    "message-flow": ("Software Design", "Decoupled message handling"),
    "nakshatra": ("E-Commerce", "Streamlined online shopping"),
    "night-sky": ("Astronomy Visualisation", "Accurate sky reconstruction"),
    "opentrading": ("FinTech", "Global trade execution"),
    "platform-shared": ("Platform Engineering", "Reusable shared services"),
    "portfolio-watcher": ("Personal Finance", "Unified portfolio view"),
    "social": ("Social Media", "Coordinated audience engagement"),
    "tax-break": ("Taxation", "Simplified tax filing"),
    "tito": ("Team Coordination", "Together in, together out"),
    "titoos": ("Agent Platforms", "Purpose-built agent runtime"),
    "travel": ("Travel", "Easier trip discovery"),
    "workout": ("Health and Fitness", "Consistent training habits"),
    "x-big-brother": ("Digital Privacy", "Control over personal data"),
}

# Repository name (case-insensitive) → a one-line "smart summary" that says what
# the project does and who it is for. Repositories without an entry fall back to
# a summary derived from their GitHub description.
REPO_SUMMARIES: dict[str, str] = {
    "5-mins": "Sends people an early alert in the minutes before a disaster reaches them, so they can act while it still matters.",
    "advantage": "Brings insurance shopping, policies and claims into a single platform instead of scattered portals.",
    "aero": "Working notes and experiments that turn aerospace engineering theory into runnable examples.",
    "agent-chaos-monkey": "Injects realistic failures into AI agents — broken connectors, expired auth, prompt injection, schema drift, truncated payloads — and judges from tool-boundary evidence whether the agent reported the truth.",
    "baby-model": "A model trained only on your own data, so answers stay grounded in what you actually gave it.",
    "basa": "A shared dashboard that keeps a family's elder-care circle coordinated around one plan.",
    "crabs": "Makes open agent access safer by putting security controls around Open Claw.",
    "design-patterns": "A practical catalogue of the design patterns engineers meet most often, with runnable examples.",
    "gitdb": "Uses a Git repository as the database, so data gets versioning, history and review for free.",
    "graphql": "Puts a GraphQL layer in front of any microservice so clients can ask for exactly the data they need.",
    "jarvis": "A personal AI companion that keeps context across everyday tasks and conversations.",
    "message-flow": "A small, dependency-free Chain of Responsibility library for decoupling message handling.",
    "nakshatra": "An online shopping portal covering browsing, cart and checkout end to end.",
    "night-sky": "Reconstructs the night sky for any date and location in a consistent panoramic style.",
    "opentrading": "A trading platform for buying and selling stocks across exchanges worldwide.",
    "platform-shared": "Shared auth, profile and notification services reused by social, travel, workout and basa.",
    "portfolio-watcher": "Gathers every financial account into one place so the whole portfolio is visible at a glance.",
    "social": "A social media manager for planning and coordinating posts across audiences.",
    "tax-break": "An online estimator that makes individual income tax easier to understand before filing.",
    "tito": "Keeps a team in sync on when everyone is in and when everyone is out — together in, together out.",
    "titoos": "An operating system for agents: a runtime purpose-built for running and supervising them.",
    "travel": "A place to explore, dream and discover trips worth taking.",
    "workout": "A weekly workout plan that makes training consistent and easy to follow.",
    "x-big-brother": "Shows how your data, Wi-Fi and mobile network watch you, and hands control back to you.",
}


def _shorten(text: str, limit: int = SUMMARY_MAX_LENGTH) -> str:
    """Return ``text`` trimmed to ``limit`` characters on a word boundary."""
    if len(text) <= limit:
        return text

    prefix = text[: limit - 1]
    trimmed = prefix.rsplit(" ", 1)[0].rstrip(" ,;:-") or prefix
    return f"{trimmed}…"


def build_summary(name: str, description: str) -> str:
    """Return the smart summary for ``name``, derived from ``description`` if unmapped."""
    curated = REPO_SUMMARIES.get(name.casefold())
    if curated:
        return curated

    cleaned = " ".join(description.split())
    if not cleaned or cleaned == NO_DESCRIPTION:
        return SUMMARY_FALLBACK

    return _shorten(cleaned)


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
        description = " ".join(str(repo.get("description") or NO_DESCRIPTION).split())

        entries.append(
            (
                name,
                f"[{name}]({html_url}) — {description}",
                build_badges(name),
                build_summary(name, description),
            )
        )

    entries.sort(key=lambda entry: entry[0].casefold())

    if not entries:
        return "1. No repositories to show yet."

    lines = []
    for position, (_, line, badges, summary) in enumerate(entries, start=1):
        marker = f"{position}. "
        indent = " " * len(marker)
        lines.append(
            f"{marker}{line} \n{indent}{badges}  \n{indent}_{SUMMARY_PREFIX} {summary}_"
        )

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
