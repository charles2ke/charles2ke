#!/usr/bin/env python3
"""Unit tests for scripts/rollout_dependabot.py."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make the scripts package importable without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.rollout_dependabot import (
    COMMIT_MESSAGE,
    CONFIG_PATH,
    GROUP_NAME,
    SCHEDULE_DAY,
    BranchNotRolloutOwnedError,
    GitHubAPIError,
    Outcome,
    detect_ecosystems,
    ecosystem_for,
    ensure_branch,
    fetch_repositories,
    main,
    render_config,
    render_summary,
    roll_out_repository,
    select_repositories,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def repo(name: str = "demo", **overrides) -> dict:
    repository = {
        "name": name,
        "full_name": f"charles2ke/{name}",
        "default_branch": "main",
        "owner": {"login": "charles2ke"},
        "fork": False,
        "archived": False,
    }
    repository.update(overrides)
    return repository


class TestEcosystemFor(unittest.TestCase):
    def test_known_manifest_names(self):
        self.assertEqual(ecosystem_for("package.json"), "npm")
        self.assertEqual(ecosystem_for("pyproject.toml"), "pip")
        self.assertEqual(ecosystem_for("go.mod"), "gomod")
        self.assertEqual(ecosystem_for("pom.xml"), "maven")

    def test_requirements_variants_are_pip(self):
        self.assertEqual(ecosystem_for("requirements.txt"), "pip")
        self.assertEqual(ecosystem_for("requirements-dev.txt"), "pip")

    def test_dockerfile_variants(self):
        self.assertEqual(ecosystem_for("Dockerfile"), "docker")
        self.assertEqual(ecosystem_for("Dockerfile.dev"), "docker")

    def test_suffix_manifests(self):
        self.assertEqual(ecosystem_for("Api.csproj"), "nuget")
        self.assertEqual(ecosystem_for("main.tf"), "terraform")

    def test_unknown_file(self):
        self.assertIsNone(ecosystem_for("README.md"))


class TestDetectEcosystems(unittest.TestCase):
    def test_workflows_enable_github_actions_at_the_root(self):
        detected = detect_ecosystems([".github/workflows/ci.yml", "README.md"])
        self.assertEqual(detected, {"github-actions": ["/"]})

    def test_root_action_file_enables_github_actions(self):
        self.assertEqual(detect_ecosystems(["action.yml"]), {"github-actions": ["/"]})

    def test_nested_action_file_is_not_github_actions(self):
        self.assertEqual(detect_ecosystems(["examples/action.yml"]), {})

    def test_non_workflow_yaml_is_ignored(self):
        self.assertEqual(detect_ecosystems([".github/release.yml"]), {})

    def test_manifest_directories_are_collected_and_sorted(self):
        detected = detect_ecosystems(
            ["server/package.json", "package.json", "client/package.json"]
        )
        self.assertEqual(detected, {"npm": ["/", "/client", "/server"]})

    def test_github_actions_is_listed_first(self):
        detected = detect_ecosystems(["package.json", ".github/workflows/ci.yml"])
        self.assertEqual(list(detected), ["github-actions", "npm"])

    def test_vendored_directories_are_skipped(self):
        detected = detect_ecosystems(
            [
                "node_modules/left-pad/package.json",
                "vendor/github.com/pkg/go.mod",
                "app/__pycache__/requirements.txt",
            ]
        )
        self.assertEqual(detected, {})

    def test_solution_directories_win_over_project_directories(self):
        detected = detect_ecosystems(["src/App.sln", "src/Api/Api.csproj"])
        self.assertEqual(detected, {"nuget": ["/src"]})

    def test_projects_outside_the_solution_tree_are_retained(self):
        detected = detect_ecosystems(["src/App.sln", "tools/Tool.csproj"])
        self.assertEqual(detected, {"nuget": ["/src", "/tools"]})

    def test_project_directories_are_used_without_a_solution(self):
        detected = detect_ecosystems(["src/Api/Api.csproj", "src/Web/Web.fsproj"])
        self.assertEqual(detected, {"nuget": ["/src/Api", "/src/Web"]})

    def test_leading_slashes_are_tolerated(self):
        self.assertEqual(detect_ecosystems(["/package.json", "", "   "]), {"npm": ["/"]})

    def test_repository_without_manifests(self):
        self.assertEqual(detect_ecosystems(["README.md", "docs/index.html"]), {})


class TestRenderConfig(unittest.TestCase):
    def test_updates_run_weekly_on_sunday(self):
        config = render_config({"npm": ["/"]})
        self.assertIn("interval: weekly", config)
        self.assertIn(f"day: {SCHEDULE_DAY}", config)
        self.assertEqual(SCHEDULE_DAY, "sunday")

    def test_single_directory_uses_the_directory_key(self):
        config = render_config({"npm": ["/"]})
        self.assertIn('    directory: "/"', config)
        self.assertNotIn("directories:", config)

    def test_multiple_directories_use_the_directories_key(self):
        config = render_config({"npm": ["/", "/client"]})
        self.assertIn("    directories:", config)
        self.assertIn('      - "/client"', config)

    def test_versioning_strategy_only_where_supported(self):
        self.assertIn("versioning-strategy: increase", render_config({"npm": ["/"]}))
        self.assertNotIn(
            "versioning-strategy", render_config({"github-actions": ["/"]})
        )
        self.assertNotIn("versioning-strategy", render_config({"nuget": ["/"]}))

    def test_updates_are_grouped_into_one_pull_request(self):
        config = render_config({"pip": ["/"]})
        self.assertIn(f"      {GROUP_NAME}:", config)
        self.assertIn('          - "*"', config)

    def test_header_names_the_managing_script(self):
        config = render_config({"npm": ["/"]})
        self.assertIn("scripts/rollout_dependabot.py", config)
        self.assertNotIn("{owner}", config)

    def test_file_is_valid_yaml_shaped_and_newline_terminated(self):
        config = render_config({"github-actions": ["/"], "npm": ["/"]})
        self.assertIn("version: 2\nupdates:\n", config)
        self.assertEqual(config.count("  - package-ecosystem:"), 2)
        self.assertTrue(config.endswith("\n"))


class TestProfileRepositoryConfig(unittest.TestCase):
    """The committed config must match what the rollout would write."""

    def test_committed_config_matches_the_renderer(self):
        paths = [
            str(path.relative_to(REPO_ROOT).as_posix())
            for path in REPO_ROOT.rglob("*")
            if path.is_file() and ".git/" not in str(path.relative_to(REPO_ROOT).as_posix())
        ]
        expected = render_config(detect_ecosystems(paths))
        self.assertEqual((REPO_ROOT / CONFIG_PATH).read_text(encoding="utf-8"), expected)


class TestEnsureBranch(unittest.TestCase):
    def test_creates_the_branch_when_it_does_not_exist(self):
        with (
            patch("scripts.rollout_dependabot.branch_head", return_value=None),
            patch("scripts.rollout_dependabot._request") as request,
        ):
            ensure_branch("charles2ke/demo", "chore/weekly-dependabot", "basesha", "t0ken")
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["method"], "POST")

    def test_does_nothing_when_branch_already_points_at_base_sha(self):
        with (
            patch("scripts.rollout_dependabot.branch_head", return_value="basesha"),
            patch("scripts.rollout_dependabot._request") as request,
        ):
            ensure_branch("charles2ke/demo", "chore/weekly-dependabot", "basesha", "t0ken")
        request.assert_not_called()

    def test_force_updates_a_branch_this_script_previously_created(self):
        with (
            patch("scripts.rollout_dependabot.branch_head", return_value="oldsha"),
            patch(
                "scripts.rollout_dependabot.commit_message", return_value=COMMIT_MESSAGE
            ),
            patch("scripts.rollout_dependabot._request") as request,
        ):
            ensure_branch("charles2ke/demo", "chore/weekly-dependabot", "basesha", "t0ken")
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["method"], "PATCH")
        self.assertEqual(request.call_args.kwargs["payload"]["force"], True)

    def test_refuses_to_force_update_a_branch_it_did_not_create(self):
        with (
            patch("scripts.rollout_dependabot.branch_head", return_value="oldsha"),
            patch("scripts.rollout_dependabot.commit_message", return_value="Unrelated work"),
            patch("scripts.rollout_dependabot._request") as request,
            self.assertRaises(BranchNotRolloutOwnedError),
        ):
            ensure_branch("charles2ke/demo", "chore/weekly-dependabot", "basesha", "t0ken")
        request.assert_not_called()


class TestRollOutRepository(unittest.TestCase):
    def roll_out(self, **kwargs):
        options = {
            "owner": "charles2ke",
            "token": "t0ken",
            "branch": "chore/weekly-dependabot",
            "direct": False,
            "dry_run": False,
        }
        options.update(kwargs)
        return roll_out_repository(repo(), **options)

    def test_repository_without_manifests_is_skipped(self):
        with patch("scripts.rollout_dependabot.fetch_paths", return_value=(["README.md"], False)):
            outcome = self.roll_out(dry_run=True)
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("no package manifests", outcome.detail)

    def test_truncated_listing_aborts_the_repository(self):
        with (
            patch("scripts.rollout_dependabot.fetch_paths", return_value=(["package.json"], True)),
            patch("scripts.rollout_dependabot.fetch_config") as fetch_config,
            patch("scripts.rollout_dependabot.write_config") as write,
        ):
            outcome = self.roll_out(dry_run=True)
        self.assertTrue(outcome.failed)
        self.assertIn("truncated", outcome.detail)
        fetch_config.assert_not_called()
        write.assert_not_called()

    def test_matching_config_is_left_alone(self):
        current = render_config({"npm": ["/"]})
        with (
            patch("scripts.rollout_dependabot.fetch_paths", return_value=(["package.json"], False)),
            patch("scripts.rollout_dependabot.fetch_config", return_value=(current, "sha1")),
            patch("scripts.rollout_dependabot.write_config") as write,
        ):
            outcome = self.roll_out()
        self.assertEqual(outcome.status, "up to date")
        write.assert_not_called()

    def test_dry_run_reports_without_writing(self):
        with (
            patch("scripts.rollout_dependabot.fetch_paths", return_value=(["package.json"], False)),
            patch("scripts.rollout_dependabot.fetch_config", return_value=(None, None)),
            patch("scripts.rollout_dependabot.write_config") as write,
            patch("scripts.rollout_dependabot.ensure_branch") as ensure,
        ):
            outcome = self.roll_out(dry_run=True)
        self.assertEqual(outcome.status, "would create")
        self.assertFalse(outcome.changed)
        write.assert_not_called()
        ensure.assert_not_called()

    def test_dry_run_reports_an_outdated_config_as_an_update(self):
        with (
            patch("scripts.rollout_dependabot.fetch_paths", return_value=(["package.json"], False)),
            patch("scripts.rollout_dependabot.fetch_config", return_value=("version: 2\n", "sha1")),
        ):
            outcome = self.roll_out(dry_run=True)
        self.assertEqual(outcome.status, "would update")

    def test_pull_request_flow_writes_to_the_working_branch(self):
        with (
            patch("scripts.rollout_dependabot.fetch_paths", return_value=(["package.json"], False)),
            patch("scripts.rollout_dependabot.fetch_config", return_value=(None, None)),
            patch("scripts.rollout_dependabot.branch_head", return_value="basesha"),
            patch("scripts.rollout_dependabot.ensure_branch") as ensure,
            patch("scripts.rollout_dependabot.write_config") as write,
            patch(
                "scripts.rollout_dependabot.open_pull_request",
                return_value="https://github.com/charles2ke/demo/pull/1",
            ) as pull_request,
        ):
            outcome = self.roll_out()

        ensure.assert_called_once_with(
            "charles2ke/demo", "chore/weekly-dependabot", "basesha", "t0ken"
        )
        self.assertEqual(write.call_args.args[1], "chore/weekly-dependabot")
        pull_request.assert_called_once()
        self.assertEqual(outcome.status, "created")
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.url, "https://github.com/charles2ke/demo/pull/1")

    def test_direct_flow_commits_to_the_default_branch(self):
        with (
            patch("scripts.rollout_dependabot.fetch_paths", return_value=(["package.json"], False)),
            patch("scripts.rollout_dependabot.fetch_config", return_value=("stale", "sha1")),
            patch("scripts.rollout_dependabot.ensure_branch") as ensure,
            patch("scripts.rollout_dependabot.write_config") as write,
            patch("scripts.rollout_dependabot.open_pull_request") as pull_request,
        ):
            outcome = self.roll_out(direct=True)

        ensure.assert_not_called()
        pull_request.assert_not_called()
        self.assertEqual(write.call_args.args[1], "main")
        self.assertEqual(write.call_args.args[3], "sha1")
        self.assertEqual(outcome.status, "updated")

    def test_missing_default_branch_fails_the_repository(self):
        with (
            patch("scripts.rollout_dependabot.fetch_paths", return_value=(["package.json"], False)),
            patch("scripts.rollout_dependabot.fetch_config", return_value=(None, None)),
            patch("scripts.rollout_dependabot.branch_head", return_value=None),
            patch("scripts.rollout_dependabot.write_config") as write,
        ):
            outcome = self.roll_out()
        self.assertTrue(outcome.failed)
        write.assert_not_called()


class TestFetchRepositories(unittest.TestCase):
    def test_forks_archived_and_other_owners_are_dropped(self):
        batches = [
            [
                repo("keep"),
                repo("forked", fork=True),
                repo("old", archived=True),
                repo("theirs", owner={"login": "someone-else"}),
            ],
            [],
        ]
        with patch("scripts.rollout_dependabot._request", side_effect=batches) as request:
            repositories = fetch_repositories("charles2ke", None)

        self.assertEqual([r["name"] for r in repositories], ["keep"])
        self.assertIn("/users/charles2ke/repos", request.call_args_list[0].args[0])

    def test_authenticated_list_is_preferred(self):
        with patch("scripts.rollout_dependabot._request", side_effect=[[repo("private-one")], []]) as request:
            repositories = fetch_repositories("charles2ke", "t0ken")

        self.assertEqual([r["name"] for r in repositories], ["private-one"])
        self.assertIn("/user/repos", request.call_args_list[0].args[0])

    def test_falls_back_to_the_public_list(self):
        responses = [GitHubAPIError("403 Forbidden"), [repo("public-one")], []]
        with patch("scripts.rollout_dependabot._request", side_effect=responses) as request:
            repositories = fetch_repositories("charles2ke", "t0ken")

        self.assertEqual([r["name"] for r in repositories], ["public-one"])
        self.assertIn("/users/charles2ke/repos", request.call_args_list[1].args[0])

    def test_failure_without_a_fallback_is_raised(self):
        with (
            patch("scripts.rollout_dependabot._request", side_effect=GitHubAPIError("boom")),
            self.assertRaises(GitHubAPIError),
        ):
            fetch_repositories("charles2ke", None)


class TestSelectRepositories(unittest.TestCase):
    def test_no_names_keeps_every_repository(self):
        repositories = [repo("one"), repo("two")]
        self.assertEqual(select_repositories(repositories, [], "charles2ke"), repositories)

    def test_names_are_matched_case_insensitively_and_ordered(self):
        repositories = [repo("one"), repo("two")]
        selected = select_repositories(repositories, ["TWO", "charles2ke/one"], "charles2ke")
        self.assertEqual([r["name"] for r in selected], ["two", "one"])

    def test_unknown_name_is_rejected(self):
        with self.assertRaises(ValueError):
            select_repositories([repo("one")], ["missing"], "charles2ke")

    def test_mismatched_owner_prefix_is_rejected(self):
        with self.assertRaises(ValueError):
            select_repositories([repo("travel")], ["other/travel"], "charles2ke")


class TestRenderSummary(unittest.TestCase):
    def test_empty_rollout(self):
        self.assertIn("No repositories", render_summary([], dry_run=False))

    def test_table_lists_every_repository(self):
        outcomes = [
            Outcome("charles2ke/one", "created", "npm", "https://example.com/pr/1"),
            Outcome("charles2ke/two", "up to date", "github-actions"),
        ]
        summary = render_summary(outcomes, dry_run=False)
        self.assertIn("| charles2ke/one | created |", summary)
        self.assertIn("https://example.com/pr/1", summary)
        self.assertIn("| charles2ke/two | up to date | github-actions |", summary)

    def test_dry_run_is_labelled(self):
        self.assertIn("dry run", render_summary([Outcome("a/b", "would create")], dry_run=True))


class TestMain(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"GITHUB_TOKEN": "t0ken"}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_missing_token_without_dry_run(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "", "ROLLOUT_TOKEN": ""}, clear=False):
            self.assertEqual(main([]), 2)

    def test_summary_is_written_to_the_step_summary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "summary.md"
            with (
                patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_path)}, clear=False),
                patch("scripts.rollout_dependabot.fetch_repositories", return_value=[repo("one")]),
                patch(
                    "scripts.rollout_dependabot.roll_out_repository",
                    return_value=Outcome("charles2ke/one", "created", "npm"),
                ),
            ):
                self.assertEqual(main(["--dry-run"]), 0)

            self.assertIn("charles2ke/one", summary_path.read_text(encoding="utf-8"))

    def test_failed_repository_sets_the_exit_code(self):
        with (
            patch("scripts.rollout_dependabot.fetch_repositories", return_value=[repo("one")]),
            patch(
                "scripts.rollout_dependabot.roll_out_repository",
                return_value=Outcome("charles2ke/one", "failed", "boom"),
            ),
        ):
            self.assertEqual(main([]), 1)

    def test_api_errors_are_reported_per_repository(self):
        with (
            patch("scripts.rollout_dependabot.fetch_repositories", return_value=[repo("one")]),
            patch(
                "scripts.rollout_dependabot.roll_out_repository",
                side_effect=GitHubAPIError("boom"),
            ),
        ):
            self.assertEqual(main([]), 1)

    def test_unknown_repository_name_is_reported(self):
        with patch("scripts.rollout_dependabot.fetch_repositories", return_value=[repo("one")]):
            self.assertEqual(main(["missing"]), 1)

    def test_pending_repositories_warn_in_actions(self):
        with (
            patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=False),
            patch("scripts.rollout_dependabot.fetch_repositories", return_value=[repo("one")]),
            patch(
                "scripts.rollout_dependabot.roll_out_repository",
                return_value=Outcome("charles2ke/one", "would create", "npm"),
            ),
            patch("builtins.print") as printed,
        ):
            self.assertEqual(main(["--dry-run"]), 0)

        warnings = [call for call in printed.call_args_list if "::warning::" in str(call)]
        self.assertTrue(warnings)


if __name__ == "__main__":
    unittest.main()
