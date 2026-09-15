#!/usr/bin/env python3
"""Unit tests for scripts/collect_failures.py."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

# Make the scripts package importable without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.collect_failures import (
    GitHubAPIError,
    build_snapshot,
    main,
    summarise_failure,
    unresolved_failures,
)


def run(
    run_id: int,
    workflow_id: int = 1,
    branch: str = "main",
    conclusion: str = "failure",
    updated_at: str = "2026-08-30T10:00:00Z",
    status: str = "completed",
) -> dict:
    return {
        "id": run_id,
        "workflow_id": workflow_id,
        "head_branch": branch,
        "conclusion": conclusion,
        "status": status,
        "updated_at": updated_at,
        "name": "CI",
        "run_number": run_id,
        "event": "push",
        "html_url": f"https://github.com/charles2ke/demo/actions/runs/{run_id}",
    }


class TestUnresolvedFailures(unittest.TestCase):
    def test_keeps_latest_failure(self):
        failures = unresolved_failures([run(1, updated_at="2026-08-30T09:00:00Z")])
        self.assertEqual([1], [failure["id"] for failure in failures])

    def test_later_success_resolves_failure(self):
        runs = [
            run(1, updated_at="2026-08-30T09:00:00Z"),
            run(2, conclusion="success", updated_at="2026-08-30T10:00:00Z"),
        ]
        self.assertEqual([], unresolved_failures(runs))

    def test_later_failure_after_success_is_unresolved(self):
        runs = [
            run(1, conclusion="success", updated_at="2026-08-30T09:00:00Z"),
            run(2, updated_at="2026-08-30T10:00:00Z"),
        ]
        self.assertEqual([2], [failure["id"] for failure in unresolved_failures(runs)])

    def test_branches_are_tracked_separately(self):
        runs = [
            run(1, branch="main", conclusion="success"),
            run(2, branch="feature"),
        ]
        self.assertEqual([2], [failure["id"] for failure in unresolved_failures(runs)])

    def test_workflows_are_tracked_separately(self):
        runs = [
            run(1, workflow_id=1, conclusion="success"),
            run(2, workflow_id=2),
        ]
        self.assertEqual([2], [failure["id"] for failure in unresolved_failures(runs)])

    def test_includes_timed_out_and_startup_failures(self):
        runs = [
            run(1, workflow_id=1, conclusion="timed_out"),
            run(2, workflow_id=2, conclusion="startup_failure"),
            run(3, workflow_id=3, conclusion="cancelled"),
        ]
        self.assertEqual(
            [1, 2],
            sorted(failure["id"] for failure in unresolved_failures(runs)),
        )

    def test_ignores_in_progress_runs(self):
        runs = [
            run(1, updated_at="2026-08-30T09:00:00Z"),
            run(2, conclusion=None, status="in_progress", updated_at="2026-08-30T11:00:00Z"),
        ]
        self.assertEqual([1], [failure["id"] for failure in unresolved_failures(runs)])

    def test_newest_failure_is_listed_first(self):
        runs = [
            run(1, workflow_id=1, updated_at="2026-08-30T09:00:00Z"),
            run(2, workflow_id=2, updated_at="2026-08-30T11:00:00Z"),
        ]
        self.assertEqual([2, 1], [failure["id"] for failure in unresolved_failures(runs)])


class TestSummariseFailure(unittest.TestCase):
    def test_projects_expected_fields(self):
        summary = summarise_failure(run(7))
        self.assertEqual(7, summary["id"])
        self.assertEqual("CI", summary["workflow"])
        self.assertEqual("main", summary["branch"])
        self.assertEqual("failure", summary["conclusion"])
        self.assertTrue(summary["url"].endswith("/runs/7"))

    def test_tolerates_missing_fields(self):
        summary = summarise_failure({"id": 9})
        self.assertEqual("Workflow", summary["workflow"])
        self.assertEqual("failure", summary["conclusion"])
        self.assertEqual("", summary["branch"])
        self.assertEqual("", summary["actor"])


class TestBuildSnapshot(unittest.TestCase):
    repositories: ClassVar[list[dict]] = [
        {
            "name": "demo",
            "full_name": "charles2ke/demo",
            "html_url": "https://github.com/charles2ke/demo",
            "description": "Demo",
            "private": False,
            "fork": False,
            "archived": False,
        },
        {
            "name": "green",
            "full_name": "charles2ke/green",
            "html_url": "https://github.com/charles2ke/green",
            "description": "All good",
            "private": False,
            "fork": False,
            "archived": False,
        },
    ]

    def _build(self):
        runs = {
            "charles2ke/demo": [run(1), run(2, workflow_id=2)],
            "charles2ke/green": [run(3, conclusion="success")],
        }
        with (
            patch(
                "scripts.collect_failures.fetch_repositories",
                return_value=self.repositories,
            ),
            patch(
                "scripts.collect_failures.fetch_runs",
                side_effect=lambda full_name, token: runs[full_name],
            ),
        ):
            return build_snapshot("charles2ke", None)

    def test_groups_failures_by_repository(self):
        snapshot = self._build()
        self.assertEqual(1, snapshot["repository_count"])
        self.assertEqual(2, snapshot["failure_count"])
        self.assertEqual("charles2ke/demo", snapshot["repositories"][0]["full_name"])

    def test_omits_repositories_without_failures(self):
        snapshot = self._build()
        names = [repo["full_name"] for repo in snapshot["repositories"]]
        self.assertNotIn("charles2ke/green", names)

    def test_includes_generated_timestamp(self):
        snapshot = self._build()
        self.assertTrue(snapshot["generated_at"].endswith("Z"))

    def test_main_writes_json_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "failures.json"
            with patch("scripts.collect_failures.build_snapshot", return_value=self._build()):
                self.assertEqual(0, main(["--output", str(output)]))

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["failure_count"])


class TestFetchRepositories(unittest.TestCase):
    def test_skips_private_forked_and_archived(self):
        from scripts.collect_failures import fetch_repositories

        page_one = [
            {"name": "public", "full_name": "o/public"},
            {"name": "private", "full_name": "o/private", "private": True},
            {"name": "fork", "full_name": "o/fork", "fork": True},
            {"name": "archived", "full_name": "o/archived", "archived": True},
        ]
        with patch("scripts.collect_failures._request", side_effect=[page_one, []]):
            repositories = fetch_repositories("o", None)

        self.assertEqual(["public"], [repo["name"] for repo in repositories])


class TestRequestRetries(unittest.TestCase):
    class _Response:
        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._body

    def _urlopen_result(self, payload: dict):
        return self._Response(json.dumps(payload).encode("utf-8"))

    def _http_error(self, code: int):
        return urllib.error.HTTPError(
            url="https://api.github.com/x",
            code=code,
            msg="Bad Gateway",
            hdrs=None,
            fp=None,
        )

    def _invalid_json_result(self):
        return self._Response(b"not json")

    def test_retries_transient_server_error(self):
        from scripts.collect_failures import _request

        responses = [self._http_error(502), self._urlopen_result({"ok": True})]
        with (
            patch("scripts.collect_failures.urllib.request.urlopen", side_effect=responses),
            patch("scripts.collect_failures.time.sleep"),
        ):
            self.assertEqual({"ok": True}, _request("https://api.github.com/x", None))

    def test_gives_up_after_max_attempts(self):
        from scripts.collect_failures import MAX_ATTEMPTS, _request

        with (
            patch(
                "scripts.collect_failures.urllib.request.urlopen",
                side_effect=[self._http_error(503) for _ in range(MAX_ATTEMPTS)],
            ) as mock_urlopen,
            patch("scripts.collect_failures.time.sleep") as mock_sleep,
            self.assertRaises(GitHubAPIError),
        ):
            _request("https://api.github.com/x", None)

        self.assertEqual(MAX_ATTEMPTS, mock_urlopen.call_count)
        self.assertEqual(
            [2.0, 4.0, 8.0, 16.0],
            [sleep_call.args[0] for sleep_call in mock_sleep.call_args_list],
        )

    def test_retries_network_error(self):
        from scripts.collect_failures import _request

        responses = [
            urllib.error.URLError("connection reset"),
            self._urlopen_result({"ok": True}),
        ]
        with (
            patch(
                "scripts.collect_failures.urllib.request.urlopen", side_effect=responses
            ) as mock_urlopen,
            patch("scripts.collect_failures.time.sleep") as mock_sleep,
        ):
            self.assertEqual({"ok": True}, _request("https://api.github.com/x", None))

        self.assertEqual(2, mock_urlopen.call_count)
        mock_sleep.assert_called_once_with(2.0)

    def test_gives_up_after_max_attempts_on_network_error(self):
        from scripts.collect_failures import MAX_ATTEMPTS, _request

        with (
            patch(
                "scripts.collect_failures.urllib.request.urlopen",
                side_effect=[
                    urllib.error.URLError("connection reset") for _ in range(MAX_ATTEMPTS)
                ],
            ) as mock_urlopen,
            patch("scripts.collect_failures.time.sleep") as mock_sleep,
            self.assertRaises(GitHubAPIError),
        ):
            _request("https://api.github.com/x", None)

        self.assertEqual(MAX_ATTEMPTS, mock_urlopen.call_count)
        self.assertEqual(
            [2.0, 4.0, 8.0, 16.0],
            [sleep_call.args[0] for sleep_call in mock_sleep.call_args_list],
        )

    def test_retries_malformed_json_response(self):
        from scripts.collect_failures import _request

        responses = [self._invalid_json_result(), self._urlopen_result({"ok": True})]
        with (
            patch(
                "scripts.collect_failures.urllib.request.urlopen", side_effect=responses
            ) as mock_urlopen,
            patch("scripts.collect_failures.time.sleep") as mock_sleep,
        ):
            self.assertEqual({"ok": True}, _request("https://api.github.com/x", None))

        self.assertEqual(2, mock_urlopen.call_count)
        mock_sleep.assert_called_once_with(2.0)

    def test_gives_up_after_max_attempts_on_malformed_json(self):
        from scripts.collect_failures import MAX_ATTEMPTS, _request

        with (
            patch(
                "scripts.collect_failures.urllib.request.urlopen",
                side_effect=[self._invalid_json_result() for _ in range(MAX_ATTEMPTS)],
            ) as mock_urlopen,
            patch("scripts.collect_failures.time.sleep") as mock_sleep,
            self.assertRaises(GitHubAPIError),
        ):
            _request("https://api.github.com/x", None)

        self.assertEqual(MAX_ATTEMPTS, mock_urlopen.call_count)
        self.assertEqual(
            [2.0, 4.0, 8.0, 16.0],
            [sleep_call.args[0] for sleep_call in mock_sleep.call_args_list],
        )

    def test_does_not_retry_client_error(self):
        from scripts.collect_failures import _request

        urlopen = patch(
            "scripts.collect_failures.urllib.request.urlopen",
            side_effect=self._http_error(404),
        )
        with (
            urlopen as mock_urlopen,
            patch("scripts.collect_failures.time.sleep"),
            self.assertRaises(GitHubAPIError),
        ):
            _request("https://api.github.com/x", None)
        self.assertEqual(1, mock_urlopen.call_count)


class TestTransientApiFailures(unittest.TestCase):
    def test_repository_error_is_skipped_and_reported(self):
        repositories = [
            {"name": "demo", "full_name": "charles2ke/demo"},
            {"name": "broken", "full_name": "charles2ke/broken"},
        ]

        def fetch_runs(full_name, token):
            if full_name == "charles2ke/broken":
                raise GitHubAPIError("GitHub API request failed: 502 Bad Gateway")
            return [run(1)]

        with (
            patch("scripts.collect_failures.fetch_repositories", return_value=repositories),
            patch("scripts.collect_failures.fetch_runs", side_effect=fetch_runs),
        ):
            snapshot = build_snapshot("charles2ke", None)

        self.assertEqual(["charles2ke/demo"], [r["full_name"] for r in snapshot["repositories"]])
        self.assertTrue(snapshot["degraded"])
        self.assertEqual(1, len(snapshot["errors"]))
        self.assertIn("charles2ke/broken", snapshot["errors"][0])

    def test_main_writes_degraded_snapshot_instead_of_failing(self):
        error = GitHubAPIError("GitHub API request failed: 502 Bad Gateway")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "failures.json"
            with patch("scripts.collect_failures.build_snapshot", side_effect=error):
                self.assertEqual(0, main(["--output", str(output)]))

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertTrue(payload["degraded"])
        self.assertEqual([], payload["repositories"])
        self.assertEqual(0, payload["failure_count"])
        self.assertIn("502 Bad Gateway", payload["errors"][0])


if __name__ == "__main__":
    unittest.main()
