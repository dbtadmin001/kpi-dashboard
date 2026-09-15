"""One poller for everybody, so the delivery view can be live without being rude.

The old design pulled GitHub Actions *inside* the page request and the browser
re-requested every 15 seconds. Two things were wrong with it.

First it was slow where it mattered: a refresh costs one call for the run list
plus one per run whose steps are worth having, so a page load could be twenty
API calls and the view only moved on a timer anyway.

Second, and worse, it silently lied. Unauthenticated GitHub allows sixty calls
an hour. At fifteen-second polling that budget is gone in under a minute, every
call after it returns 403, and the collector swallowed the error - so the page
went on showing a build that had finished long ago, with nothing to say it had
stopped listening. That is what "the UI doesn't refresh" actually was.

So: exactly one poller for the whole process, no matter how many tabs are open.
It polls fast while something is building and slowly when nothing is, it watches
its own rate-limit budget rather than discovering it as a 403, and whatever it
learns is published to browsers over SSE within a quarter second. Failure is
part of the published state, not an exception nobody sees.
"""
import os
import threading
import time

# How often to ask GitHub. The browser is not on this clock - it is pushed to.
BUSY = 3.0          # something is in progress and somebody is watching it
IDLE = 20.0         # nothing is building; this is just "did a run start?"
FLOOR = 2.0         # never faster than this, whatever the caller asks for


class Poller:
    """A thread, a version counter, and an honest status.

    Nothing here is awaited. The SSE route watches `version`, which is enough:
    a change means new rows are already committed, and 250ms of latency on a
    build status is invisible next to the fifteen seconds it replaces.
    """

    def __init__(self, conn, interval=None):
        self.conn = conn
        self.version = 0                  # bumped whenever the stored state changed
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        self.stopping = False
        self.override = float(interval) if interval else None
        self.status = {"state": "starting", "detail": "", "last_ok": None,
                       "last_error": None, "interval": IDLE, "remaining": None,
                       "repo": None, "authenticated": False, "running": 0}

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return self
        self.stopping = False
        self.thread = threading.Thread(target=self._loop, name="build-poller", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stopping = True
        self.wake.set()

    def nudge(self):
        """Poll now rather than at the next tick - for a user who pressed refresh."""
        self.wake.set()

    # -- the loop ----------------------------------------------------------
    def _loop(self):
        while not self.stopping:
            delay = self._once()
            self.wake.wait(delay)
            self.wake.clear()

    def _once(self):
        """One pull. Returns how long to wait before the next one."""
        from .collectors.builds import BuildCollector, RateLimited
        from . import store

        collector = BuildCollector()
        self.status["repo"] = collector.repo
        self.status["authenticated"] = collector.authenticated
        if not collector.repo:
            self._set("no repo", "no GitHub remote on origin, so there is nothing to poll")
            return 60.0
        try:
            runs = collector.runs(20)
        except RateLimited as limited:
            # Say so, and wait for the window rather than burning 403s into it.
            wait = max(FLOOR, min(limited.retry_after, 900))
            self._set("rate limited",
                      f"GitHub budget spent; resuming in {int(wait)}s"
                      + ("" if collector.authenticated else ". Sign in with `gh auth login` for 5000/hour"))
            self.status["last_error"] = time.time()
            return wait
        except Exception as error:                 # noqa: BLE001 - published, not raised
            self._set("unreachable", f"{type(error).__name__}: {str(error)[:120]}")
            self.status["last_error"] = time.time()
            return 30.0

        changed = 0
        with self.lock:
            for record in runs:
                if store.put_build(self.conn, record):
                    changed += 1
            self.conn.commit()
        running = sum(1 for r in runs if r.get("status") != "completed")
        self.status.update(running=running, remaining=collector.remaining)
        self._set("live", f"{len(runs)} runs, {running} in progress")
        self.status["last_ok"] = time.time()
        if changed:
            self.version += 1              # this is what wakes every open tab

        interval = self.override or (BUSY if running else IDLE)
        # Stay inside the budget rather than finding its edge. Below fifty calls
        # remaining, stretch out - a live view is not worth locking ourselves out.
        if collector.remaining is not None and collector.remaining < 50:
            interval = max(interval, 60.0)
        return max(FLOOR, interval)

    def _set(self, state, detail):
        self.status.update(state=state, detail=detail)


_poller = None


def poller(conn=None):
    global _poller
    if _poller is None and conn is not None:
        _poller = Poller(conn, os.environ.get("CATALOG_POLL_SECONDS")).start()
    return _poller
