import copy
import json
import threading
import time
from collections import OrderedDict


class SharedCache:
    def __init__(self, redis_url="", default_ttl=24, memory_entries=384):
        self.redis_url = (redis_url or "").strip()
        self.default_ttl = max(1, int(default_ttl))
        self.memory_entries = max(64, int(memory_entries))
        self._memory = OrderedDict()
        self._lock = threading.Lock()
        self._redis = None
        if self.redis_url:
            try:
                import redis
                client = redis.Redis.from_url(
                    self.redis_url,
                    socket_connect_timeout=1.0,
                    socket_timeout=1.0,
                    health_check_interval=30,
                    socket_keepalive=True,
                    decode_responses=True,
                )
                client.ping()
                self._redis = client
            except Exception:
                self._redis = None

    @property
    def mode(self):
        return "redis" if self._redis is not None else "memory"

    def get(self, key):
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
                return json.loads(raw) if raw else None
            except Exception:
                pass
        now = time.monotonic()
        with self._lock:
            item = self._memory.get(key)
            if not item:
                return None
            exp, payload = item
            if exp < now:
                self._memory.pop(key, None)
                return None
            self._memory.move_to_end(key)
        # deepcopy is materially cheaper than serialize+parse for the large
        # route geometry objects stored in the memory fallback, and doing it
        # outside the lock keeps cache hits concurrent.
        return copy.deepcopy(payload)

    def set(self, key, payload, ttl=None):
        ttl = max(1, int(ttl or self.default_ttl))
        if self._redis is not None:
            try:
                self._redis.setex(key, ttl, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
                return
            except Exception:
                pass
        cloned = copy.deepcopy(payload)
        with self._lock:
            self._memory[key] = (time.monotonic() + ttl, cloned)
            self._memory.move_to_end(key)
            while len(self._memory) > self.memory_entries:
                self._memory.popitem(last=False)

    def delete(self, key):
        if self._redis is not None:
            try:
                self._redis.delete(key)
            except Exception:
                pass
        with self._lock:
            self._memory.pop(key, None)
