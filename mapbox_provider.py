import copy
import threading
import time
from collections import OrderedDict

import requests
from requests.adapters import HTTPAdapter

from geo import clamp, haversine_m, sanitize_bearing, sanitize_speed


class MapboxProviderError(RuntimeError):
    def __init__(self, message, status_code=None, retryable=True):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = bool(retryable)


class MapboxProvider:
    DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox"

    def __init__(self, token, language="pt-BR", timeout_s=7.5,
                 connect_timeout_s=2.2, provider_cache_ttl_s=8,
                 max_concurrency=8, cache_entries=256):
        self.token = (token or "").strip()
        self.language = language or "pt-BR"
        self.timeout_s = float(timeout_s)
        self.connect_timeout_s = float(connect_timeout_s)
        self.provider_cache_ttl_s = max(1, int(provider_cache_ttl_s))
        self.cache_entries = max(32, int(cache_entries))
        self.session = requests.Session()
        # A single shared connection pool avoids TCP/TLS setup for every variant.
        adapter = HTTPAdapter(pool_connections=max(4, int(max_concurrency)), pool_maxsize=max(8, int(max_concurrency) + 4), max_retries=0, pool_block=True)
        self.session.mount("https://", adapter)
        self.session.headers.update({"User-Agent": "VAIGO-Route-Node/4", "Accept": "application/json"})
        self._request_slots = threading.BoundedSemaphore(max(2, int(max_concurrency)))
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._inflight = {}
        self._inflight_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._calls = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._coalesced = 0
        self._errors = 0
        self._timeouts = 0
        self._active = 0
        self._peak_active = 0

    def ready(self):
        return bool(self.token and "your_mapbox" not in self.token.lower())

    def snapshot(self):
        with self._stats_lock:
            return {
                "provider_calls": self._calls,
                "provider_cache_hits": self._cache_hits,
                "provider_cache_misses": self._cache_misses,
                "provider_coalesced": self._coalesced,
                "provider_errors": self._errors,
                "provider_timeouts": self._timeouts,
                "provider_active": self._active,
                "provider_peak_active": self._peak_active,
            }

    def _stat(self, field, delta=1):
        with self._stats_lock:
            setattr(self, field, getattr(self, field) + delta)

    def _active_delta(self, delta):
        with self._stats_lock:
            self._active = max(0, self._active + delta)
            self._peak_active = max(self._peak_active, self._active)

    def _cache_get(self, key):
        now = time.monotonic()
        with self._cache_lock:
            item = self._cache.get(key)
            if not item:
                return None
            ts, payload = item
            if now - ts > self.provider_cache_ttl_s:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
        return copy.deepcopy(payload)

    def _cache_put(self, key, payload):
        cloned = copy.deepcopy(payload)
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), cloned)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_entries:
                self._cache.popitem(last=False)

    def _get(self, url, params, timeout=None):
        if not self.ready():
            raise MapboxProviderError("MAPBOX_ACCESS_TOKEN não configurado neste node.", retryable=False)
        params = dict(params or {})
        params["access_token"] = self.token
        key = (url, tuple(sorted((str(k), str(v)) for k, v in params.items())))
        cached = self._cache_get(key)
        if cached is not None:
            self._stat("_cache_hits")
            return cached
        self._stat("_cache_misses")

        owner = False
        with self._inflight_lock:
            job = self._inflight.get(key)
            if job is None:
                job = {"event": threading.Event(), "result": None, "error": None}
                self._inflight[key] = job
                owner = True
            else:
                self._stat("_coalesced")
        read_timeout = max(2.0, float(timeout or self.timeout_s))
        if owner:
            try:
                acquired = self._request_slots.acquire(timeout=min(read_timeout, 4.0))
                if not acquired:
                    raise MapboxProviderError("Fila de requisições Mapbox cheia.", retryable=True)
                self._active_delta(1)
                try:
                    self._stat("_calls")
                    r = self.session.get(url, params=params, timeout=(self.connect_timeout_s, read_timeout))
                finally:
                    self._active_delta(-1)
                    self._request_slots.release()
                status = int(r.status_code)
                if status >= 400:
                    message = "Mapbox respondeu HTTP %s" % status
                    try:
                        data = r.json() or {}
                        message = str(data.get("message") or message)
                    except Exception:
                        pass
                    raise MapboxProviderError(message, status_code=status, retryable=status >= 429)
                result = r.json() or {}
                job["result"] = result
                self._cache_put(key, result)
            except requests.Timeout as exc:
                self._stat("_timeouts")
                self._stat("_errors")
                job["error"] = MapboxProviderError("Timeout consultando Mapbox.", retryable=True)
            except MapboxProviderError as exc:
                self._stat("_errors")
                job["error"] = exc
            except Exception as exc:
                self._stat("_errors")
                job["error"] = MapboxProviderError(f"Mapbox indisponível: {type(exc).__name__}", retryable=True)
            finally:
                job["event"].set()
                with self._inflight_lock:
                    self._inflight.pop(key, None)
        else:
            if not job["event"].wait(timeout=read_timeout + self.connect_timeout_s + .5):
                raise MapboxProviderError("Timeout aguardando requisição Mapbox compartilhada.", retryable=True)

        if job.get("error"):
            raise job["error"]
        if job.get("result") is None:
            raise MapboxProviderError("Mapbox não retornou conteúdo utilizável.", retryable=True)
        return copy.deepcopy(job["result"])

    @staticmethod
    def _profile(profile):
        return {
            "walking": "walking",
            "cycling": "cycling",
            "driving": "driving-traffic",
            "motorcycle": "driving-traffic",
        }.get(profile, "driving-traffic")

    def routes(self, start_lon, start_lat, end_lon, end_lat, profile="driving", depart_at="now",
               alternatives=True, exclusions=None, extra_excludes=None, start_bearing=None,
               start_speed=None, reroute=False):
        used_profile = self._profile(profile)
        coords = f"{float(start_lon):.6f},{float(start_lat):.6f};{float(end_lon):.6f},{float(end_lat):.6f}"
        params = {
            "alternatives": "true" if alternatives else "false",
            "steps": "true",
            "geometries": "geojson",
            "overview": "full",
            "language": self.language,
            "annotations": "distance,duration,speed",
        }
        if used_profile == "driving-traffic":
            params["annotations"] = "distance,duration,speed,congestion,congestion_numeric"
            params["depart_at"] = depart_at if depart_at else "now"
            bearing = sanitize_bearing(start_bearing)
            speed = sanitize_speed(start_speed)
            if bearing is not None and speed is not None and speed >= 1.2:
                params["bearings"] = f"{bearing:.0f},70;"
                params["continue_straight"] = "true"
                if reroute:
                    params["avoid_maneuver_radius"] = str(int(clamp(45 + speed * 7.0, 45, 160)))
            excludes = []
            for value in extra_excludes or []:
                value = str(value or "").strip().lower()
                if value in {"unpaved", "toll", "ferry", "motorway", "tunnel", "cash_only_tolls"} and value not in excludes:
                    excludes.append(value)
            for point in (exclusions or [])[:18]:
                try:
                    lon, lat = float(point[0]), float(point[1])
                    excludes.append(f"point({lon:.6f} {lat:.6f})")
                except Exception:
                    pass
            if excludes:
                params["exclude"] = ",".join(excludes)

        # Keep the worker below the Central's typical route timeout. Long trips
        # get a small allowance but never the old 26-32 s blocking window.
        direct_km = haversine_m(start_lat, start_lon, end_lat, end_lon) / 1000.0
        timeout = min(9.0, max(self.timeout_s, self.timeout_s + 1.0 if direct_km > 600 else self.timeout_s))
        url = f"{self.DIRECTIONS_URL}/{used_profile}/{coords}"
        try:
            data = self._get(url, params, timeout=timeout)
        except MapboxProviderError as exc:
            # Only retry with non-traffic driving on fast client/input errors.
            # Timeouts/network failures should fail fast so the Central can move
            # to another node instead of waiting for a second slow HTTP request.
            if used_profile != "driving-traffic" or exc.status_code not in {400, 404, 422}:
                raise
            fallback = dict(params)
            fallback.pop("depart_at", None)
            fallback["annotations"] = "distance,duration,speed"
            used_profile = "driving"
            data = self._get(f"{self.DIRECTIONS_URL}/driving/{coords}", fallback, timeout=timeout)

        if data.get("code") != "Ok":
            raise MapboxProviderError(str(data.get("message") or "Mapbox não encontrou uma rota."), retryable=False)
        routes = data.get("routes") or []
        if not routes:
            raise MapboxProviderError("Mapbox não retornou alternativas de rota.", retryable=False)
        for route in routes:
            route["_profile_used"] = used_profile
            route["_provider"] = "mapbox"
        return routes
