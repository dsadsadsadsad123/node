import copy
import threading
import time
from datetime import datetime, timedelta, timezone


class ContextStore:
    """Optional read-only access to VAIGO shared PostgreSQL context.

    V2 keeps a very short local context cache and can load a lightweight context
    for ETA-only calculations. That avoids reopening PostgreSQL and rerunning the
    same corridor queries for every mode of the same destination.
    """

    def __init__(self, database_url="", enabled=True, cache_ttl_s=15):
        self.database_url = (database_url or "").strip()
        self.enabled = bool(enabled and self.database_url)
        self.cache_ttl_s = max(3, min(60, int(cache_ttl_s)))
        self._cache = {}
        self._cache_lock = threading.Lock()

    def ready(self):
        return self.enabled

    def _connect(self):
        if not self.enabled:
            raise RuntimeError("PostgreSQL context disabled")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as exc:
            raise RuntimeError("psycopg não está instalado") from exc
        return psycopg.connect(self.database_url, connect_timeout=3, row_factory=dict_row, autocommit=True)

    @staticmethod
    def _bounds(start, end, pad=0.04):
        min_lat = min(float(start["lat"]), float(end["lat"])) - pad
        max_lat = max(float(start["lat"]), float(end["lat"])) + pad
        min_lon = min(float(start["lon"]), float(end["lon"])) - pad
        max_lon = max(float(start["lon"]), float(end["lon"])) + pad
        return min_lat, min_lon, max_lat, max_lon

    def _key(self, start, end, lightweight=False):
        vals = (
            round(float(start["lat"]), 4), round(float(start["lon"]), 4),
            round(float(end["lat"]), 4), round(float(end["lon"]), 4),
            bool(lightweight),
        )
        return vals

    def _cache_get(self, key):
        now = time.monotonic()
        with self._cache_lock:
            item = self._cache.get(key)
            if not item:
                return None
            ts, payload = item
            if now - ts > self.cache_ttl_s:
                self._cache.pop(key, None)
                return None
        return copy.deepcopy(payload)

    def _cache_put(self, key, payload):
        cloned = copy.deepcopy(payload)
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), cloned)
            if len(self._cache) > 96:
                oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:24]
                for k, _ in oldest:
                    self._cache.pop(k, None)

    def load(self, start, end, lightweight=False):
        if not self.enabled:
            return {"reports": [], "risk_zones": [], "flow_samples": [], "source": "none"}
        cache_key = self._key(start, end, lightweight)
        cached = self._cache_get(cache_key)
        if cached is not None:
            cached["context_cache_hit"] = True
            return cached

        min_lat, min_lon, max_lat, max_lon = self._bounds(start, end)
        cutoff_reports = (datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0).isoformat()
        cutoff_flow = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        reports, zones, flow = [], [], []
        try:
            with self._connect() as db:
                with db.cursor() as cur:
                    # Even the ETA-only path keeps admin block zones. Reports and
                    # live-flow samples are unnecessary because Mapbox traffic ETA
                    # already drives the fastest selector.
                    cur.execute(
                        """
                        SELECT id,name,risk_type,latitude,longitude,radius_m,level_cap,confidence,source,source_url,
                               start_hour,end_hour,neighborhood,city,state,danger_level,block_routes,active
                        FROM risk_zones
                        WHERE active=1 AND latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
                        ORDER BY block_routes DESC, confidence DESC, id DESC
                        LIMIT 300
                        """,
                        (min_lat - .05, max_lat + .05, min_lon - .05, max_lon + .05),
                    )
                    zones = list(cur.fetchall() or [])
                    if not lightweight:
                        cur.execute(
                            """
                            SELECT id,category,title,description,severity,latitude,longitude,address,status,created_at,expires_at,confirmations
                            FROM reports
                            WHERE status='active' AND created_at >= %s
                              AND latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
                              AND (expires_at IS NULL OR expires_at > %s)
                            ORDER BY severity DESC, confirmations DESC, created_at DESC
                            LIMIT 400
                            """,
                            (cutoff_reports, min_lat, max_lat, min_lon, max_lon, now),
                        )
                        reports = list(cur.fetchall() or [])
                        try:
                            cur.execute(
                                """
                                SELECT cell_lat,cell_lon,direction_bucket,AVG(speed_kmh) avg_speed,
                                       COUNT(*) samples,COUNT(DISTINCT source_hash) sources,MAX(created_at) updated_at
                                FROM flow_samples
                                WHERE created_at >= %s AND cell_lat BETWEEN %s AND %s AND cell_lon BETWEEN %s AND %s
                                GROUP BY cell_lat,cell_lon,direction_bucket
                                HAVING COUNT(DISTINCT source_hash) >= 3
                                LIMIT 260
                                """,
                                (cutoff_flow, min_lat, max_lat, min_lon, max_lon),
                            )
                            flow = list(cur.fetchall() or [])
                        except Exception:
                            flow = []
            payload = {"reports": reports, "risk_zones": zones, "flow_samples": flow, "source": "postgresql-light" if lightweight else "postgresql", "context_cache_hit": False}
            self._cache_put(cache_key, payload)
            return payload
        except Exception as exc:
            return {"reports": [], "risk_zones": [], "flow_samples": [], "source": "postgresql-error", "error": str(exc)[:240], "context_cache_hit": False}
