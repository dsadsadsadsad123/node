import math
import os
import threading
import time
from collections import deque


class CapacityGate:
    def __init__(self, capacity=4):
        self.capacity = max(1, int(capacity))
        self._sem = threading.BoundedSemaphore(self.capacity)
        self._lock = threading.Lock()
        self._active = 0
        self._accepted = 0
        self._rejected = 0

    def try_acquire(self):
        ok = self._sem.acquire(blocking=False)
        with self._lock:
            if ok:
                self._active += 1
                self._accepted += 1
            else:
                self._rejected += 1
        return ok

    def release(self):
        released = False
        with self._lock:
            if self._active > 0:
                self._active -= 1
                released = True
        if released:
            self._sem.release()

    def snapshot(self):
        with self._lock:
            return {
                "active_jobs": self._active,
                "capacity": self.capacity,
                "available_slots": max(0, self.capacity - self._active),
                "accepted_jobs": self._accepted,
                "capacity_rejections": self._rejected,
                "occupancy_pct": round(100 * self._active / self.capacity, 1),
            }


class InflightCoordinator:
    """Coalesces identical route calculations inside one worker process.

    Mapbox requests were already coalesced in the provider, but two identical
    route jobs could still repeat scoring, safety work and candidate assembly.
    Waiters share the owner's result and do not consume another capacity slot.
    """

    def __init__(self, max_entries=256):
        self.max_entries = max(16, int(max_entries))
        self._lock = threading.Lock()
        self._jobs = {}

    def claim(self, key):
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                job["waiters"] += 1
                return False, job
            job = {
                "event": threading.Event(),
                "created": time.monotonic(),
                "result": None,
                "waiters": 0,
            }
            self._jobs[key] = job
            # Entries normally live only for a few seconds. This is a defensive
            # bound for unexpected worker exceptions.
            if len(self._jobs) > self.max_entries:
                stale = sorted(self._jobs.items(), key=lambda kv: kv[1]["created"])[: max(1, self.max_entries // 8)]
                for old_key, old_job in stale:
                    if old_key != key and old_job["event"].is_set():
                        self._jobs.pop(old_key, None)
            return True, job

    def finish(self, key, result):
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                return
            job["result"] = result
            job["event"].set()
            self._jobs.pop(key, None)

    @staticmethod
    def wait(job, timeout_s):
        if not job["event"].wait(timeout=max(.05, float(timeout_s))):
            return None
        return job.get("result")

    def snapshot(self):
        with self._lock:
            return {"inflight_route_jobs": len(self._jobs), "inflight_waiters": sum(int(j.get("waiters") or 0) for j in self._jobs.values())}


class Metrics:
    def __init__(self, window=600):
        self.started_at = time.time()
        self._window = max(120, int(window))
        self._lock = threading.Lock()
        self._events = deque(maxlen=4000)  # (timestamp, duration_ms, success, kind)
        self._cache_hits = 0
        self._cache_misses = 0
        self._coalesced = 0
        self._inflight_timeouts = 0

    def record(self, duration_ms, success=True, kind="route"):
        now = time.time()
        with self._lock:
            self._events.append((now, float(duration_ms), bool(success), str(kind)))
            self._trim(now)

    def cache_hit(self):
        with self._lock:
            self._cache_hits += 1

    def cache_miss(self):
        with self._lock:
            self._cache_misses += 1

    def coalesced(self):
        with self._lock:
            self._coalesced += 1

    def inflight_timeout(self):
        with self._lock:
            self._inflight_timeouts += 1

    def _trim(self, now):
        cutoff = now - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def snapshot(self):
        now = time.time()
        with self._lock:
            self._trim(now)
            events = list(self._events)
            cache_hits, cache_misses = self._cache_hits, self._cache_misses
            coalesced, inflight_timeouts = self._coalesced, self._inflight_timeouts
        recent = [e for e in events if e[0] >= now - 60]
        route_recent = [e for e in recent if e[3] == "route" or str(e[3]).startswith("precalc")]
        durations = sorted(e[1] for e in route_recent)
        avg = round(sum(durations) / len(durations), 1) if durations else 0.0
        if durations:
            # Nearest-rank p95 without importing statistics/numpy.
            idx = min(len(durations) - 1, max(0, int(math.ceil(.95 * len(durations))) - 1))
            p95 = round(durations[idx], 1)
        else:
            p95 = 0.0
        errors = sum(1 for e in route_recent if not e[2])
        total_cache = cache_hits + cache_misses
        return {
            "uptime_s": round(now - self.started_at),
            "requests_min": len(route_recent),
            "response_avg_ms": avg,
            "response_p95_ms": p95,
            "error_rate_pct": round(100 * errors / max(1, len(route_recent)), 2),
            "cache_hits": cache_hits,
            "cache_misses": cache_misses,
            "cache_hit_pct": round(100 * cache_hits / max(1, total_cache), 1),
            "coalesced_requests": coalesced,
            "inflight_wait_timeouts": inflight_timeouts,
        }


_SYSTEM_LOCK = threading.Lock()
_SYSTEM_CACHE = {"at": 0.0, "payload": None}


def system_snapshot(cache_s=1.0):
    """Return system telemetry without invoking psutil repeatedly per request."""
    now = time.monotonic()
    with _SYSTEM_LOCK:
        cached = _SYSTEM_CACHE.get("payload")
        if cached is not None and now - float(_SYSTEM_CACHE.get("at") or 0) < cache_s:
            return dict(cached)
    try:
        import psutil
        proc = psutil.Process(os.getpid())
        mem = psutil.virtual_memory()
        payload = {
            "cpu_pct": round(float(psutil.cpu_percent(interval=None)), 1),
            "memory_pct": round(float(mem.percent), 1),
            "process_memory_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
            "pid": os.getpid(),
        }
    except Exception:
        payload = {"cpu_pct": None, "memory_pct": None, "process_memory_mb": None, "pid": os.getpid()}
    with _SYSTEM_LOCK:
        _SYSTEM_CACHE["at"] = now
        _SYSTEM_CACHE["payload"] = dict(payload)
    return payload
