"""Build runs, so the control plane can show what is happening now.

Reads GitHub Actions. A public repository serves workflow runs without a token,
which is what makes this useful on day one; GITHUB_TOKEN is picked up when set,
for a private repo or a higher rate limit.

Steps are fetched for runs that are in progress or that failed - the two cases
where somebody is actually looking. A run that succeeded twenty minutes ago does
not need its step list pulled on every refresh.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime

API = "https://api.github.com"


def _repo():
    if os.environ.get("CATALOG_REPO"):
        return os.environ["CATALOG_REPO"]
    try:
        remote = subprocess.run(["git", "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
        if "github.com" in remote:
            return remote.split("github.com")[1].lstrip(":/").removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _get(path):
    request = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "nda-infrastructure-catalog"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def _epoch(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class BuildCollector:
    """Not a resource collector - it fills the `build` table the control plane reads."""
    name = "builds"
    provider = "builds"
    kinds = []
    capabilities = {"delivery"}

    def __init__(self, repo=None):
        self.repo = repo or _repo()

    def health(self):
        if not self.repo:
            return {"ok": False, "detail": "no GitHub remote on origin"}
        try:
            data = _get(f"/repos/{self.repo}/actions/runs?per_page=1")
            return {"ok": True,
                    "detail": f"{self.repo} · {data.get('total_count', 0)} runs"}
        except urllib.error.HTTPError as error:
            hint = " (private repo - set GITHUB_TOKEN)" if error.code in (401, 404) else ""
            return {"ok": False, "detail": f"HTTP {error.code}{hint}"}
        except Exception as error:                 # noqa: BLE001
            return {"ok": False, "detail": str(error)[:80]}

    def runs(self, limit=25):
        if not self.repo:
            return []
        data = _get(f"/repos/{self.repo}/actions/runs?per_page={limit}")
        out = []
        for run in data.get("workflow_runs", []):
            record = {
                "id": str(run["id"]),
                "source": "github-actions",
                "name": run.get("name"),
                "branch": run.get("head_branch"),
                "sha": (run.get("head_sha") or "")[:12],
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "started": _epoch(run.get("run_started_at") or run.get("created_at")),
                "finished": _epoch(run.get("updated_at")) if run.get("status") == "completed" else None,
                "url": run.get("html_url"),
                "steps": [],
            }
            # Only pull steps where someone is actually looking: running, or broken.
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                record["steps"] = self._steps(run["id"])
            out.append(record)
        return out

    def _steps(self, run_id):
        try:
            jobs = _get(f"/repos/{self.repo}/actions/runs/{run_id}/jobs")
        except Exception:                          # noqa: BLE001 - detail is optional
            return []
        steps = []
        for job in jobs.get("jobs", []):
            steps.append({"job": job.get("name"), "status": job.get("status"),
                          "conclusion": job.get("conclusion"),
                          "steps": [{"name": s.get("name"), "conclusion": s.get("conclusion")}
                                    for s in job.get("steps", [])]})
        return steps

    def collect(self):
        return []                                  # resources come from elsewhere


def refresh(conn, limit=25):
    """Pull the latest runs into the store. Safe to call on every page load."""
    from .. import store
    collector = BuildCollector()
    if not collector.repo:
        return 0
    count = 0
    for record in collector.runs(limit):
        store.put_build(conn, record)
        count += 1
    conn.commit()
    return count
