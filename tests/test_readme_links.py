#!/usr/bin/env python3
"""Link checks for the Markdown files in this repository.

The profile README is published twice — on the GitHub profile page and on the
GitHub Pages site — so a broken link is immediately visible to visitors. These
tests keep every link honest:

* the structural checks always run and need no network, and
* the reachability checks run against every host that can be reached (all of
  them in CI), failing only on genuinely dead links.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import unittest
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"

# The GitHub Pages site for this project repository, as reported by the
# `github-pages` deployment environment.
PAGES_BASE_URL = "https://charles2ke.github.io/charles2ke/"
PAGES_INDEX = "index.html"

# Markdown inline links: [text](target "optional title").
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# Links and images written as raw HTML inside Markdown.
HTML_LINK = re.compile(r"(?:href|src)=\"([^\"]+)\"")

USER_AGENT = "charles2ke-link-check"
REQUEST_TIMEOUT = 20
# Status codes that prove a link is broken. Anything else (including the 403,
# 429 and 999 responses that sites such as LinkedIn return to automated
# clients) means the target exists.
BROKEN_STATUSES = frozenset({404, 410})


def markdown_files() -> list[Path]:
    """Return every Markdown file tracked in the repository."""
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.md")
        if ".git" not in path.parts and "node_modules" not in path.parts
    )


def iter_links(path: Path) -> list[tuple[str, int]]:
    """Return ``(target, line_number)`` for every link in ``path``."""
    links: list[tuple[str, int]] = []

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for pattern in (MARKDOWN_LINK, HTML_LINK):
            links.extend((match, number) for match in pattern.findall(line))

    return links


def all_links() -> list[tuple[Path, str, int]]:
    """Return ``(file, target, line_number)`` for every link in the repository."""
    return [
        (path, target, number)
        for path in markdown_files()
        for target, number in iter_links(path)
    ]


def published_pages_files() -> set[str]:
    """Return the file names the Pages workflow publishes to the site root.

    Parsing the workflow — rather than hard-coding a list — means a renamed or
    dropped page is caught here instead of by a visitor hitting a 404.
    """
    workflow = PAGES_WORKFLOW.read_text(encoding="utf-8")

    published = set(re.findall(r"--output\s+_site/(\S+)", workflow))
    for sources in re.findall(r"\bcp\s+(.+?)\s+_site/", workflow):
        published.update(Path(source).name for source in sources.split())

    return published


def unique_urls() -> list[str]:
    """Return every distinct HTTP(S) URL used across the Markdown files."""
    return sorted(
        {
            target
            for _, target, _ in all_links()
            if target.startswith(("http://", "https://"))
        }
    )


def _host_is_reachable(host: str) -> bool:
    if host.lower() == "localhost":
        return False

    try:
        addresses = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False

    return bool(addresses) and all(
        ipaddress.ip_address(address[4][0]).is_global for address in addresses
    )


def _status_code(url: str) -> int | None:
    """Return the HTTP status for ``url``, or ``None`` when it cannot be read."""
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme: {url}")

    for method in ("HEAD", "GET"):
        request = urllib.request.Request(
            url,
            method=method,
            headers={"User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.status
        except urllib.error.HTTPError as error:
            # Some hosts reject HEAD outright; retry those with a GET.
            if method == "HEAD" and error.code in {403, 405, 501}:
                continue
            return error.code
        except (urllib.error.URLError, OSError, TimeoutError):
            return None

    return None


class TestLinkStructure(unittest.TestCase):
    """Checks that hold without touching the network."""

    def test_repository_has_links_to_check(self):
        self.assertTrue(all_links(), "no Markdown links were found to check")

    def test_urls_are_https(self):
        insecure = [
            f"{path.relative_to(REPO_ROOT)}:{number} {target}"
            for path, target, number in all_links()
            if target.startswith("http://")
        ]
        self.assertEqual([], insecure, "links must use https")

    def test_urls_are_well_formed(self):
        malformed = []

        for path, target, number in all_links():
            if not target.startswith("https://"):
                continue

            parsed = urllib.parse.urlsplit(target)
            if not parsed.netloc or " " in target:
                malformed.append(f"{path.relative_to(REPO_ROOT)}:{number} {target}")

        self.assertEqual([], malformed, "links must be well-formed URLs")

    def test_relative_links_exist(self):
        missing = []

        for path, target, number in all_links():
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
                continue

            resolved = (path.parent / urllib.parse.unquote(target.split("#")[0])).resolve()
            if not resolved.exists():
                missing.append(f"{path.relative_to(REPO_ROOT)}:{number} {target}")

        self.assertEqual([], missing, "relative links must point at existing files")

    def test_mailto_links_are_addresses(self):
        invalid = [
            f"{path.relative_to(REPO_ROOT)}:{number} {target}"
            for path, target, number in all_links()
            if target.startswith("mailto:")
            and not re.fullmatch(r"mailto:[^@\s]+@[^@\s]+\.[^@\s]+", target)
        ]
        self.assertEqual([], invalid, "mailto links must contain an email address")

    def test_readme_links_to_the_failure_dashboard(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"{PAGES_BASE_URL}failures.html", readme)


class TestGitHubPagesLinks(unittest.TestCase):
    """Links into the Pages site must match what the workflow publishes."""

    def _pages_links(self) -> list[tuple[Path, str, int]]:
        return [
            (path, target, number)
            for path, target, number in all_links()
            if urllib.parse.urlsplit(target).netloc == "charles2ke.github.io"
        ]

    def test_pages_links_use_the_project_site_base_url(self):
        wrong_base = [
            f"{path.relative_to(REPO_ROOT)}:{number} {target}"
            for path, target, number in self._pages_links()
            if not target.startswith(PAGES_BASE_URL)
        ]
        self.assertEqual([], wrong_base, f"Pages links must start with {PAGES_BASE_URL}")

    def test_pages_links_point_at_published_files(self):
        published = published_pages_files()
        unpublished = []

        for path, target, number in self._pages_links():
            page = target[len(PAGES_BASE_URL) :].split("#")[0].split("?")[0]
            page = page or PAGES_INDEX
            if page not in published:
                unpublished.append(f"{path.relative_to(REPO_ROOT)}:{number} {target}")

        self.assertEqual(
            [],
            unpublished,
            f"Pages links must be published by {PAGES_WORKFLOW.name}: "
            f"{sorted(published)}",
        )

    def test_workflow_publishes_the_dashboard(self):
        self.assertIn("failures.html", published_pages_files())

    def test_published_sources_exist(self):
        workflow = PAGES_WORKFLOW.read_text(encoding="utf-8")
        missing = [
            source
            for sources in re.findall(r"\bcp\s+(.+?)\s+_site/", workflow)
            for source in sources.split()
            if not (REPO_ROOT / source).exists()
        ]
        self.assertEqual([], missing, "the Pages workflow copies files that do not exist")

    def test_workflow_redeploys_when_the_site_changes(self):
        workflow = PAGES_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("site/**", workflow)


class TestLinksAreReachable(unittest.TestCase):
    """Fetch every link and fail on the ones that are gone.

    Hosts that cannot be resolved are reported as skipped rather than broken so
    the suite still passes in sandboxes with restricted network access.
    """

    @classmethod
    def setUpClass(cls):
        cls.urls = unique_urls()
        hosts = {
            host
            for url in cls.urls
            if (host := urllib.parse.urlsplit(url).hostname) is not None
        }

        with ThreadPoolExecutor(max_workers=8) as pool:
            reachable = dict(zip(hosts, pool.map(_host_is_reachable, hosts)))

        cls.hosts = {host for host, ok in reachable.items() if ok}
        if not cls.hosts:
            raise unittest.SkipTest("no network access for link checking")

    def _checkable_urls(self) -> list[str]:
        return [
            url
            for url in self.urls
            if urllib.parse.urlsplit(url).hostname in self.hosts
        ]

    def test_links_are_not_dead(self):
        urls = self._checkable_urls()

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(_status_code, urls))

        broken = [
            f"{url} -> {status}"
            for url, status in zip(urls, statuses)
            if status in BROKEN_STATUSES
        ]
        self.assertEqual([], broken, "links must not be broken")

    def test_failure_dashboard_is_published(self):
        url = f"{PAGES_BASE_URL}failures.html"
        if urllib.parse.urlsplit(url).netloc not in self.hosts:
            self.skipTest("GitHub Pages host is not reachable")

        self.assertNotIn(
            _status_code(url),
            BROKEN_STATUSES,
            f"{url} is not published; check the 'Deploy to GitHub Pages' workflow",
        )


class TestReachabilitySafety(unittest.TestCase):
    """Network checks must not probe private CI infrastructure."""

    def test_localhost_is_not_reachable(self):
        self.assertFalse(_host_is_reachable("localhost"))

    def test_private_addresses_are_not_reachable(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]
        with patch("socket.getaddrinfo", return_value=addresses):
            self.assertFalse(_host_is_reachable("example.com"))


if __name__ == "__main__":
    unittest.main()
