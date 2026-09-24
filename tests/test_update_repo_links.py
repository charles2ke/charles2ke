#!/usr/bin/env python3
"""Unit tests for scripts/update_repo_links.py."""

from __future__ import annotations

import re
import sys
import textwrap
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

# Make the scripts package importable without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.update_repo_links import (
    DEFAULT_BADGES,
    NO_DESCRIPTION,
    PROFILE_REPO,
    REPO_BADGES,
    SECTION_END,
    SECTION_START,
    _badge,
    build_badges,
    build_repo_lines,
    update_readme,
)


class TestBuildRepoLines(unittest.TestCase):
    def test_skips_profile_repo(self):
        repos = [{"full_name": PROFILE_REPO, "name": "charles2ke", "html_url": "https://github.com/charles2ke/charles2ke", "fork": False, "description": None}]
        result = build_repo_lines(repos)
        self.assertNotIn("charles2ke/charles2ke", result)

    def test_skips_forks(self):
        repos = [{"full_name": "charles2ke/fork-repo", "name": "fork-repo", "html_url": "https://github.com/charles2ke/fork-repo", "fork": True, "description": "A fork"}]
        result = build_repo_lines(repos)
        self.assertNotIn("fork-repo", result)

    def test_includes_normal_repo(self):
        repos = [{"full_name": "charles2ke/my-project", "name": "my-project", "html_url": "https://github.com/charles2ke/my-project", "fork": False, "description": "My cool project"}]
        result = build_repo_lines(repos)
        self.assertIn("[my-project](https://github.com/charles2ke/my-project)", result)
        self.assertIn("My cool project", result)

    def test_fallback_description_when_none(self):
        repos = [{"full_name": "charles2ke/silent", "name": "silent", "html_url": "https://github.com/charles2ke/silent", "fork": False, "description": None}]
        result = build_repo_lines(repos)
        self.assertIn(NO_DESCRIPTION, result)

    def test_empty_repo_list_returns_placeholder(self):
        result = build_repo_lines([])
        self.assertEqual(result, "1. No repositories to show yet.")

    def test_multiple_repos_are_sorted_alphabetically(self):
        repos = [
            {"full_name": "charles2ke/beta", "name": "beta", "html_url": "https://github.com/charles2ke/beta", "fork": False, "description": "Beta"},
            {"full_name": "charles2ke/alpha", "name": "alpha", "html_url": "https://github.com/charles2ke/alpha", "fork": False, "description": "Alpha"},
        ]
        result = build_repo_lines(repos)
        lines = [line for line in result.splitlines() if re.match(r"\d+\. ", line)]
        self.assertEqual(len(lines), 2)
        self.assertIn("alpha", lines[0])
        self.assertIn("beta", lines[1])

    def test_entries_are_numbered(self):
        repos = [
            {"full_name": f"charles2ke/repo{index}", "name": f"repo{index}", "html_url": f"https://github.com/charles2ke/repo{index}", "fork": False, "description": "Desc"}
            for index in range(1, 4)
        ]
        result = build_repo_lines(repos)
        lines = [line for line in result.splitlines() if re.match(r"\d+\. ", line)]
        self.assertTrue(lines[0].startswith("1. ["))
        self.assertTrue(lines[1].startswith("2. ["))
        self.assertTrue(lines[2].startswith("3. ["))

    def test_sorting_is_case_insensitive(self):
        repos = [
            {"full_name": "charles2ke/zebra", "name": "zebra", "html_url": "https://github.com/charles2ke/zebra", "fork": False, "description": "Zebra"},
            {"full_name": "charles2ke/Apple", "name": "Apple", "html_url": "https://github.com/charles2ke/Apple", "fork": False, "description": "Apple"},
            {"full_name": "charles2ke/banana", "name": "banana", "html_url": "https://github.com/charles2ke/banana", "fork": False, "description": "Banana"},
        ]
        result = build_repo_lines(repos)
        lines = [line for line in result.splitlines() if re.match(r"\d+\. ", line)]
        self.assertIn("Apple", lines[0])
        self.assertIn("banana", lines[1])
        self.assertIn("zebra", lines[2])

    def test_whitespace_in_description_is_collapsed(self):
        repos = [{"full_name": "charles2ke/spaced", "name": "spaced", "html_url": "https://github.com/charles2ke/spaced", "fork": False, "description": "  too   many   spaces  "}]
        result = build_repo_lines(repos)
        self.assertIn("too many spaces", result)
        self.assertNotIn("  ", result.split("— ")[1].splitlines()[0])


class TestBuildBadges(unittest.TestCase):
    def test_known_repo_uses_mapped_field_and_value(self):
        field, value = REPO_BADGES["workout"]
        badges = build_badges("workout")
        self.assertIn(f'alt="Field: {field}"', badges)
        self.assertIn(f'alt="Value: {value}"', badges)

    def test_lookup_is_case_insensitive(self):
        self.assertEqual(build_badges("WORKOUT"), build_badges("workout"))

    def test_unknown_repo_falls_back_to_defaults(self):
        badges = build_badges("unlisted-repo")
        self.assertIn(f'alt="Field: {DEFAULT_BADGES[0]}"', badges)
        self.assertIn(f'alt="Value: {DEFAULT_BADGES[1]}"', badges)

    def test_hyphens_are_escaped_in_badge_url(self):
        badges = build_badges("Nakshatra")
        self.assertIn("Field-E--Commerce-", badges)

    def test_spaces_are_url_encoded(self):
        badges = build_badges("workout")
        self.assertIn("Health%20and%20Fitness", badges)

    def test_underscores_are_escaped_in_badge_url(self):
        badge = _badge("Field", "Data_Engineering", "0A66C2")
        self.assertIn("Field-Data__Engineering-", badge)


class TestNewRepoBadgeMappings(unittest.TestCase):
    """Guards the badge mappings added for Advantage, platform-shared and X-Big-Brother."""

    EXPECTED: ClassVar[dict[str, tuple[str, str]]] = {
        "advantage": ("Insurance", "One-stop policy management"),
        "platform-shared": ("Platform Engineering", "Reusable shared services"),
        "x-big-brother": ("Digital Privacy", "Control over personal data"),
    }

    def test_mappings_are_registered(self):
        for key, expected in self.EXPECTED.items():
            with self.subTest(repo=key):
                self.assertEqual(REPO_BADGES.get(key), expected)

    def test_badges_render_mapped_field_and_value(self):
        for key, (field, value) in self.EXPECTED.items():
            with self.subTest(repo=key):
                badges = build_badges(key)
                self.assertIn(f'alt="Field: {field}"', badges)
                self.assertIn(f'alt="Value: {value}"', badges)

    def test_badges_do_not_fall_back_to_defaults(self):
        for key in self.EXPECTED:
            with self.subTest(repo=key):
                badges = build_badges(key)
                self.assertNotIn(f'alt="Field: {DEFAULT_BADGES[0]}"', badges)
                self.assertNotIn(f'alt="Value: {DEFAULT_BADGES[1]}"', badges)

    def test_lookup_matches_actual_repository_casing(self):
        for repo_name in ("Advantage", "platform-shared", "X-Big-Brother"):
            with self.subTest(repo=repo_name):
                self.assertEqual(build_badges(repo_name), build_badges(repo_name.lower()))

    def test_hyphenated_value_is_escaped_in_badge_url(self):
        self.assertIn("Value-One--stop%20policy%20management-", build_badges("advantage"))

    def test_repo_lines_use_mapped_badges(self):
        repos = [
            {
                "full_name": f"charles2ke/{name}",
                "name": name,
                "html_url": f"https://github.com/charles2ke/{name}",
                "fork": False,
                "description": "Desc",
            }
            for name in ("Advantage", "platform-shared", "X-Big-Brother")
        ]
        result = build_repo_lines(repos)
        for field, value in self.EXPECTED.values():
            self.assertIn(f'alt="Field: {field}"', result)
            self.assertIn(f'alt="Value: {value}"', result)


class TestLatestRepoBadgeMappings(unittest.TestCase):
    """Guards the badge mappings added for aero, jarvis and tito."""

    EXPECTED: ClassVar[dict[str, tuple[str, str]]] = {
        "aero": ("Aerospace Engineering", "Applied flight fundamentals"),
        "jarvis": ("Personal AI", "Always-on personal companion"),
        "tito": ("Team Coordination", "Together in, together out"),
    }

    def test_mappings_are_registered(self):
        for key, expected in self.EXPECTED.items():
            with self.subTest(repo=key):
                self.assertEqual(REPO_BADGES.get(key), expected)

    def test_badges_do_not_fall_back_to_defaults(self):
        for key in self.EXPECTED:
            with self.subTest(repo=key):
                badges = build_badges(key)
                self.assertNotIn(f'alt="Field: {DEFAULT_BADGES[0]}"', badges)
                self.assertNotIn(f'alt="Value: {DEFAULT_BADGES[1]}"', badges)

    def test_tito_and_titoos_have_distinct_badges(self):
        self.assertNotEqual(build_badges("tito"), build_badges("TitoOS"))

    def test_readme_lists_every_mapped_repository(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        section = readme.split(SECTION_START)[1].split(SECTION_END)[0]
        for name in ("aero", "jarvis", "tito"):
            with self.subTest(repo=name):
                self.assertIn(f"https://github.com/charles2ke/{name})", section)

    def test_readme_section_has_no_curated_summary_line(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        section = readme.split(SECTION_START)[1].split(SECTION_END)[0]
        self.assertNotIn("🧠", section)


class TestEntryRendering(unittest.TestCase):
    def _repo(self, name: str, description: str = "Desc"):
        return {
            "full_name": f"charles2ke/{name}",
            "name": name,
            "html_url": f"https://github.com/charles2ke/{name}",
            "fork": False,
            "description": description,
        }

    def test_entry_is_description_and_badges_only(self):
        result = build_repo_lines([self._repo("workout", "My weekly workout plan")])
        lines = result.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("1. [workout]"))
        self.assertIn("My weekly workout plan", lines[0])
        self.assertTrue(lines[0].endswith("<br>"))
        self.assertIn('img alt="Field:', lines[1])

    def test_description_markdown_is_escaped(self):
        result = build_repo_lines([self._repo("workout", "Uses *fast* [tools]")])
        self.assertIn(r"— Uses \*fast\* \[tools\]<br>", result)


class TestUpdateReadme(unittest.TestCase):
    def _make_readme(self, body: str) -> str:
        return textwrap.dedent(f"""\
            # Profile

            {SECTION_START}
            {body}
            {SECTION_END}

            ## Footer
        """)

    def test_replaces_section_when_changed(self, tmp_path: Path | None = None):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            readme = Path(td) / "README.md"
            readme.write_text(self._make_readme("- [old](https://old.example.com) — old"), encoding="utf-8")

            with patch("scripts.update_repo_links.README_PATH", readme):
                changed = update_readme("- [new](https://new.example.com) — new")

            self.assertTrue(changed)
            content = readme.read_text(encoding="utf-8")
            self.assertIn("[new]", content)
            self.assertNotIn("[old]", content)

    def test_no_change_when_section_already_matches(self):
        import tempfile

        body = "- [same](https://same.example.com) — same"
        with tempfile.TemporaryDirectory() as td:
            readme = Path(td) / "README.md"
            readme.write_text(self._make_readme(body), encoding="utf-8")

            with patch("scripts.update_repo_links.README_PATH", readme):
                changed = update_readme(body)

            self.assertFalse(changed)

    def test_section_markers_preserved_after_update(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            readme = Path(td) / "README.md"
            readme.write_text(self._make_readme("- [x](https://x.example.com) — x"), encoding="utf-8")

            with patch("scripts.update_repo_links.README_PATH", readme):
                update_readme("- [y](https://y.example.com) — y")

            content = readme.read_text(encoding="utf-8")
            self.assertIn(SECTION_START, content)
            self.assertIn(SECTION_END, content)


class TestFetchRepositories(unittest.TestCase):
    def _make_response(self, data):
        mock = MagicMock()
        mock.__enter__ = MagicMock(return_value=mock)
        mock.__exit__ = MagicMock(return_value=False)
        mock.read = MagicMock(return_value=b"")
        # json.load reads from the file-like object; patch that separately.
        return mock

    def test_stops_on_empty_page(self):

        responses = [
            [{"full_name": "charles2ke/repo-one", "name": "repo-one", "html_url": "...", "fork": False, "description": "One"}],
            [],  # empty → stop
        ]
        call_count = 0

        def fake_urlopen(request):
            nonlocal call_count
            response = MagicMock()
            response.__enter__ = MagicMock(return_value=response)
            response.__exit__ = MagicMock(return_value=False)
            # Make json.load work by patching the return value
            response._data = responses[call_count]
            call_count += 1
            return response

        with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("json.load", side_effect=lambda r: r._data):
            from scripts.update_repo_links import fetch_repositories
            repos = fetch_repositories("charles2ke")

        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["name"], "repo-one")


if __name__ == "__main__":
    unittest.main()
