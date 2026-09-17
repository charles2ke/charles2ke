#!/usr/bin/env python3
"""Unit tests for scripts/auto_release.py."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Make the scripts package importable without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.auto_release import (
    Decision,
    Repository,
    evaluate,
    is_bot_commit,
    main,
    matches_excluded_paths,
    next_version,
    parse_version,
    render_summary,
    wants_major_bump,
)

NOW = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.timezone.utc)


def commit(
    sha: str = "abc1234",
    message: str = "Add a feature",
    date: str = "2026-09-01T10:00:00Z",
    login: str = "charles2ke",
    author_type: str = "User",
) -> dict:
    return {
        "sha": sha,
        "author": {"login": login, "type": author_type},
        "commit": {
            "message": message,
            "author": {"name": login, "date": date},
            "committer": {"name": login, "date": date},
        },
    }


class FakeRepository(Repository):
    """A Repository that answers from canned data instead of the API."""

    def __init__(
        self,
        *,
        release: dict | None = None,
        tags: list[str] | None = None,
        commits: list[dict] | None = None,
        files: dict[str, list[dict]] | None = None,
        check_runs: list[dict] | None = None,
        status: str = "success",
    ):
        super().__init__("charles2ke/demo", "token")
        self._release = release
        self._tags = tags or []
        self._commits = commits or []
        self._files = files or {}
        self._check_runs = check_runs or []
        self._status = status
        self.created: list[tuple[str, str]] = []

    def default_branch(self) -> str:
        return "main"

    def latest_release(self) -> dict | None:
        return self._release

    def tags(self) -> list[dict]:
        return [{"name": name} for name in self._tags]

    def compare(self, base: str, head: str) -> dict:
        return {"ahead_by": len(self._commits), "commits": self._commits}

    def commits(self, ref: str, per_page: int = 100) -> list[dict]:
        return list(reversed(self._commits))

    def commit(self, sha: str) -> dict:
        return {"sha": sha, "files": self._files.get(sha, [])}

    def check_runs(self, ref: str) -> list[dict]:
        return self._check_runs

    def combined_status(self, ref: str) -> str:
        return self._status

    def create_release(self, tag: str, target: str) -> dict:
        self.created.append((tag, target))
        return {"html_url": f"https://github.com/charles2ke/demo/releases/tag/{tag}"}


class TestVersions(unittest.TestCase):
    def test_parses_major_only_and_major_minor(self):
        self.assertEqual((1, 0), parse_version("v1"))
        self.assertEqual((1, 5), parse_version("v1.5"))
        self.assertIsNone(parse_version("1.5"))
        self.assertIsNone(parse_version("release-1"))

    def test_minor_bump_follows_existing_scheme(self):
        self.assertEqual("v1.1", next_version("v1", "minor", set()))
        self.assertEqual("v1.6", next_version("v1.5", "minor", set()))
        self.assertEqual("v2.1", next_version("v2.0", "minor", set()))

    def test_major_bump_resets_minor(self):
        self.assertEqual("v2.0", next_version("v1.5", "major", set()))
        self.assertEqual("v2.0", next_version("v1", "major", set()))

    def test_first_release_is_v1(self):
        self.assertEqual("v1", next_version(None, "minor", set()))

    def test_skips_tags_that_already_exist(self):
        self.assertEqual("v1.2", next_version("v1", "minor", {"v1.1"}))
        self.assertEqual("v1.1", next_version(None, "minor", {"v1"}))

    def test_rejects_unrecognised_tag(self):
        with self.assertRaises(ValueError):
            next_version("release-3", "minor", set())

    def test_detects_breaking_changes(self):
        self.assertTrue(wants_major_bump([commit(message="feat!: drop the old API")]))
        self.assertTrue(
            wants_major_bump([commit(message="feat: rework\n\nBREAKING CHANGE: config moved")])
        )
        self.assertFalse(wants_major_bump([commit(message="fix: typo")]))


class TestFilters(unittest.TestCase):
    def test_identifies_bot_commits(self):
        self.assertTrue(is_bot_commit(commit(login="dependabot[bot]")))
        self.assertTrue(is_bot_commit(commit(login="copilot", author_type="Bot")))
        self.assertFalse(is_bot_commit(commit()))

    def test_excluded_paths_need_every_file_to_match(self):
        files = [{"filename": "README.md"}, {"filename": "docs/guide.md"}]
        self.assertTrue(matches_excluded_paths(files, ["README.md", "docs"]))
        self.assertFalse(matches_excluded_paths(files, ["README.md"]))
        self.assertFalse(matches_excluded_paths(files, []))

    def test_prefix_match_is_path_aware(self):
        files = [{"filename": "docsite/app.js"}]
        self.assertFalse(matches_excluded_paths(files, ["docs"]))


class TestEvaluate(unittest.TestCase):
    def _evaluate(self, repository: FakeRepository, **overrides):
        options = {
            "min_age_days": 7.0,
            "settle_hours": 1.0,
            "bump": "minor",
            "exclude_paths": [],
            "skip_bot_commits": True,
            "require_green_checks": True,
            "dry_run": False,
            "now": NOW,
        }
        options.update(overrides)
        return evaluate(repository, **options)

    def _released(self, **kwargs) -> dict:
        payload = {"tag_name": "v1.1", "published_at": "2026-09-01T09:00:00Z"}
        payload.update(kwargs)
        return payload

    def test_cuts_a_release_when_there_is_new_work(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1", "v1.1"],
            commits=[commit("a1"), commit("b2")],
        )
        decision = self._evaluate(repository)

        self.assertTrue(decision.released)
        self.assertEqual("v1.2", decision.tag)
        self.assertEqual([("v1.2", "main")], repository.created)
        self.assertEqual(2, decision.commit_count)

    def test_skips_when_nothing_is_unreleased(self):
        repository = FakeRepository(release=self._released(), tags=["v1.1"], commits=[])
        decision = self._evaluate(repository)

        self.assertFalse(decision.released)
        self.assertIn("nothing to release", decision.reason)
        self.assertEqual([], repository.created)

    def test_skips_when_previous_release_is_too_recent(self):
        repository = FakeRepository(
            release=self._released(published_at="2026-09-15T12:00:00Z"),
            tags=["v1.1"],
            commits=[commit("a1")],
        )
        decision = self._evaluate(repository)

        self.assertFalse(decision.released)
        self.assertIn("too soon", decision.reason)
        self.assertEqual([], repository.created)

    def test_skips_when_newest_commit_has_not_settled(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1.1"],
            commits=[commit("a1", date="2026-09-17T11:45:00Z")],
        )
        decision = self._evaluate(repository)

        self.assertFalse(decision.released)
        self.assertIn("not settled", decision.reason)

    def test_skips_when_all_commits_are_bots(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1.1"],
            commits=[commit("a1", login="dependabot[bot]")],
        )
        decision = self._evaluate(repository)

        self.assertFalse(decision.released)
        self.assertIn("excluded", decision.reason)

    def test_skips_when_all_commits_touch_excluded_paths_only(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1.1"],
            commits=[commit("a1")],
            files={"a1": [{"filename": "README.md"}]},
        )
        decision = self._evaluate(repository, exclude_paths=["README.md"])

        self.assertFalse(decision.released)
        self.assertIn("excluded", decision.reason)

    def test_releases_when_only_some_commits_are_excluded(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1.1"],
            commits=[commit("a1"), commit("b2", login="dependabot[bot]")],
        )
        decision = self._evaluate(repository)

        self.assertTrue(decision.released)
        self.assertEqual(1, decision.commit_count)

    def test_skips_when_checks_are_failing(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1.1"],
            commits=[commit("a1")],
            check_runs=[{"name": "CI", "status": "completed", "conclusion": "failure"}],
        )
        decision = self._evaluate(repository)

        self.assertFalse(decision.released)
        self.assertIn("checks are not green", decision.reason)
        self.assertEqual([], repository.created)

    def test_skips_while_checks_are_still_running(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1.1"],
            commits=[commit("a1")],
            check_runs=[{"name": "CI", "status": "in_progress"}],
        )
        decision = self._evaluate(repository)

        self.assertFalse(decision.released)
        self.assertIn("still running", decision.reason)

    def test_releases_with_red_checks_when_allowed(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1.1"],
            commits=[commit("a1")],
            check_runs=[{"name": "CI", "status": "completed", "conclusion": "failure"}],
        )
        decision = self._evaluate(repository, require_green_checks=False)

        self.assertTrue(decision.released)

    def test_first_release_is_cut_for_untagged_repository(self):
        repository = FakeRepository(release=None, tags=[], commits=[commit("a1")])
        decision = self._evaluate(repository)

        self.assertTrue(decision.released)
        self.assertEqual("v1", decision.tag)

    def test_auto_bump_promotes_breaking_changes(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1.1"],
            commits=[commit("a1", message="feat!: new storage layout")],
        )
        decision = self._evaluate(repository, bump="auto")

        self.assertTrue(decision.released)
        self.assertEqual("v2.0", decision.tag)

    def test_dry_run_reports_without_creating_a_release(self):
        repository = FakeRepository(
            release=self._released(),
            tags=["v1.1"],
            commits=[commit("a1")],
        )
        decision = self._evaluate(repository, dry_run=True)

        self.assertFalse(decision.released)
        self.assertEqual("v1.2", decision.tag)
        self.assertIn("ready for release", decision.reason)
        self.assertEqual([], repository.created)

    def test_falls_back_to_tags_when_there_is_no_release(self):
        repository = FakeRepository(release=None, tags=["v1", "v1.3"], commits=[commit("a1")])
        decision = self._evaluate(repository)

        self.assertEqual("v1.3", decision.previous_tag)
        self.assertEqual("v1.4", decision.tag)


class TestSummary(unittest.TestCase):
    def test_summary_names_the_tag_and_commits(self):
        decision = Decision(
            released=True,
            reason="1 commit(s) included",
            tag="v1.2",
            previous_tag="v1.1",
            commit_count=1,
            commits=[commit("a1", message="Add a feature")],
            url="https://github.com/charles2ke/demo/releases/tag/v1.2",
        )
        summary = render_summary("charles2ke/demo", decision)

        self.assertIn("Released **v1.2**", summary)
        self.assertIn("Previous release: v1.1", summary)
        self.assertIn("Add a feature", summary)
        self.assertIn("releases/tag/v1.2", summary)

    def test_summary_states_the_skip_reason(self):
        decision = Decision(released=False, reason="nothing to release: main matches v1.1")
        summary = render_summary("charles2ke/demo", decision)

        self.assertIn("No release cut", summary)
        self.assertIn("nothing to release", summary)
        self.assertIn("Previous release: _none_", summary)


class TestMain(unittest.TestCase):
    def test_requires_a_repository(self):
        with patch.dict("os.environ", {"GITHUB_REPOSITORY": ""}, clear=False):
            self.assertEqual(2, main([]))

    def test_requires_a_token_outside_dry_runs(self):
        with patch.dict(
            "os.environ",
            {"GITHUB_TOKEN": "", "RELEASE_TOKEN": "", "GITHUB_REPOSITORY": ""},
            clear=False,
        ):
            self.assertEqual(2, main(["--repository", "charles2ke/demo"]))

    def test_writes_summary_and_outputs(self):
        decision = Decision(
            released=True,
            reason="1 commit(s) included",
            tag="v1.2",
            previous_tag="v1.1",
            commit_count=1,
        )
        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "t"}, clear=False),
            patch("scripts.auto_release.evaluate", return_value=decision),
            patch("scripts.auto_release.write_summary") as summary,
            patch("scripts.auto_release.write_outputs") as outputs,
        ):
            self.assertEqual(0, main(["--repository", "charles2ke/demo"]))

        summary.assert_called_once()
        outputs.assert_called_once()

    def test_reports_api_failures(self):
        from scripts.auto_release import GitHubAPIError

        with (
            patch.dict("os.environ", {"GITHUB_TOKEN": "t"}, clear=False),
            patch("scripts.auto_release.evaluate", side_effect=GitHubAPIError("boom")),
        ):
            self.assertEqual(1, main(["--repository", "charles2ke/demo"]))


if __name__ == "__main__":
    unittest.main()
