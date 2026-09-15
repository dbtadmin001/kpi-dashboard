"""Bounded readiness and eventual-consistency polling with useful failure causes."""
import time


def until(label, check, timeout=300, interval=3):
    deadline = time.monotonic() + timeout
    last = "condition was false"
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except Exception as exc:
            last = f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}"
        time.sleep(interval)
    raise RuntimeError(f"{label} did not become ready within {timeout}s: {last}")
