"""The database's own quality report for a published pathway (``wikipathways-collection``).

WikiPathways runs a separate test suite over every published pathway — 155 tests in 22 classes:
unmapped data nodes, outdated data sources, interactions involving labels, identifier formats — and
publishes the result per pathway as ``reports/WP<n>.md`` (the curation badge on the site reads the
``.json`` beside it). The portal's own rules are a smaller, offline set. Showing both side by side
on an update is what stops the two quality systems from disagreeing silently: a curator sees what
the database already reports about the pathway being edited.

Read anonymously from ``raw.githubusercontent.com`` (the repository is public) and cached on disk
for a day; fetched by the background loop, never on a page load.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx

logger = logging.getLogger("wpsubmit.collection")

_OK_GROUP = re.compile(r"^\* (\w+): all (\d+) tests? OK!")
_GROUP = re.compile(r"^\* (\w+)\s*$")
_TEST = re.compile(r"^ {4}\* (\w+): (.*)$")
_PROBLEM = re.compile(r"^ {8}\* \[(.+?)\]\(#")
_TOTAL = re.compile(r"^\* Number of tests: (\d+)")
_FAILS = re.compile(r"^\* Number of fails: (\d+)")


def parse_report(markdown: str) -> dict:
    """``{"tests", "fails", "problems": [{"group", "test", "text"}]}`` from one report."""
    tests = fails = None
    problems: list[dict] = []
    group = test = None
    for line in markdown.splitlines():
        if line.startswith("## Fails"):
            break
        m = _TOTAL.match(line)
        if m:
            tests = int(m.group(1))
            continue
        m = _FAILS.match(line)
        if m:
            fails = int(m.group(1))
            continue
        if _OK_GROUP.match(line):
            group = test = None
            continue
        m = _GROUP.match(line)
        if m:
            group, test = m.group(1), None
            continue
        m = _TEST.match(line)
        if m and group:
            test = m.group(1)
            continue
        m = _PROBLEM.match(line)
        if m and group and test:
            problems.append({"group": group, "test": test, "text": m.group(1)})
    return {"tests": tests, "fails": fails, "problems": problems}


class CollectionReports:
    def __init__(
        self,
        *,
        repo: str,
        branch: str = "main",
        cache_dir: str | Path,
        ttl_seconds: int = 86_400,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._repo = repo
        self._branch = branch
        self._dir = Path(cache_dir)
        self._ttl = ttl_seconds
        self._client = httpx.Client(transport=transport, timeout=10.0, follow_redirects=True)

    def page_url(self, wpid: int) -> str:
        return f"https://github.com/{self._repo}/blob/{self._branch}/reports/WP{wpid}.md"

    def _path(self, wpid: int) -> Path:
        return self._dir / f"WP{int(wpid)}.json"

    def cached(self, wpid: int) -> dict | None:
        """What the last fetch found, from disk only. None when never fetched or not published."""
        try:
            data = json.loads(self._path(wpid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        report = data.get("report") if isinstance(data, dict) else None
        if not isinstance(report, dict):
            return None
        return {**report, "url": self.page_url(wpid), "fetched_at": data.get("fetched_at")}

    def refresh(self, wpid: int) -> bool:
        """Fetch WP<wpid>'s report unless a fresh copy is on disk. True when it fetched."""
        path = self._path(wpid)
        try:
            age = time.time() - json.loads(path.read_text(encoding="utf-8"))["fetched_at"]
            if age < self._ttl:
                return False
        except (OSError, ValueError, KeyError, TypeError):
            pass
        url = (
            f"https://raw.githubusercontent.com/{self._repo}/{self._branch}/reports/WP{int(wpid)}.md"
        )
        try:
            resp = self._client.get(url)
        except httpx.HTTPError:
            logger.warning("could not fetch %s", url, exc_info=True)
            return False
        if resp.status_code == 404:
            report = None  # not published, or not tested yet: remembered, so not re-asked hourly
        elif resp.status_code != 200:
            logger.warning("fetching %s answered %s", url, resp.status_code)
            return False
        else:
            report = parse_report(resp.text)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"fetched_at": time.time(), "report": report}), encoding="utf-8"
            )
        except OSError:
            return False
        return True
