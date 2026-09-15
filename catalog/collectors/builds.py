"""Build runs, so the control plane can show what is happening now.

Reads GitHub Actions. A public repository serves workflow runs without a token,
which is what makes this useful on day one - but anonymous GitHub allows sixty
calls an hour and one page refresh can cost twenty, so in practice a token is
not optional. Rather than demand one, `_token` finds the one already on the
machine: GITHUB_TOKEN, then `gh auth token`, then the git credential helper that
`git push` is already using for github.com. All three are the same credential
for the same host, and any of them raises the budget to 5000 an hour.

Running out is reported, never swallowed. A view that quietly stops updating is
worse than one that says it has stopped.

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


class RateLimited(Exception):
    """The budget is spent. Carries how long until it is not."""

    def __init__(self, retry_after, authenticated):
        super().__init__(f"GitHub rate limit reached; resets in {int(retry_after)}s")
        self.retry_after = retry_after
        self.authenticated = authenticated


_token_cache = []

# What `git credential fill` expects on stdin to be asked about github.com.
CREDENTIAL_QUERY = "\n".join(["protocol=https", "host=github.com", "", ""])


def _token():
    """Whatever GitHub credential this machine already has, in order of intent.

    The git credential helper is last because it is the least explicit - but it
    is also the one that is always there on a machine that has pushed, and it is
    only ever replayed to the host it was stored for.
    """
    if _token_cache:
        return _token_cache[0] or None
    found = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not found:
        for argv, feed in ((["gh", "auth", "token"], None),
                           (["git", "credential", "fill"], CREDENTIAL_QUERY)):
            try:
                done = subprocess.run(argv, input=feed, capture_output=True, text=True,
                                      timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            if done.returncode != 0:
                continue
            out = done.stdout.strip()
            if argv[0] == "git":
                out = next((l[len("password="):] for l in out.splitlines()
                            if l.startswith("password=")), "")
            if out:
                found = out.strip()
                break
    _token_cache.append(found or "")
    return found or None


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


# What the last call was told about the budget, so the poller can pace itself
# instead of discovering the limit as a 403.
BUDGET = {"remaining": None, "reset": None, "authenticated": False}


def _get(path):
    request = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "nda-infrastructure-catalog"})
    token = _token()
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    BUDGET["authenticated"] = bool(token)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            _remember(response.headers)
            return json.load(response)
    except urllib.error.HTTPError as error:
        _remember(error.headers)
        # 403 and 429 both mean "not now"; only a spent budget says how long.
        if error.code in (403, 429) and error.headers.get("X-RateLimit-Remaining") == "0":
            reset = BUDGET["reset"] or (time.time() + 60)
            raise RateLimited(max(1.0, reset - time.time()), BUDGET["authenticated"]) from None
        raise


def _remember(headers):
    try:
        BUDGET["remaining"] = int(headers.get("X-RateLimit-Remaining"))
        BUDGET["reset"] = int(headers.get("X-RateLimit-Reset"))
    except (TypeError, ValueError):
        pass


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

    @property
    def remaining(self):
        return BUDGET["remaining"]

    @property
    def authenticated(self):
        return BUDGET["authenticated"] or bool(_token())

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
        except RateLimited:
            raise                                  # the budget is not a detail
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
    """Pull the latest runs into the store.

    Kept for `catalog.cli collect` and for an explicit refresh. The delivery view
    no longer calls it per request - catalog/live.py polls once for every open
    tab, which is what makes a faster refresh affordable.
    """
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
