import os
from dataclasses import dataclass


def _int(name, default, lo=None, hi=None):
    try:
        value = int(os.environ.get(name, default))
    except Exception:
        value = int(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _float(name, default, lo=None, hi=None):
    try:
        value = float(os.environ.get(name, default))
    except Exception:
        value = float(default)
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _bool(name, default=False):
    raw = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    node_id: str
    node_name: str
    node_region: str
    node_capacity: int
    central_api_secret: str
    mapbox_token: str
    mapbox_language: str
    database_url: str
    redis_url: str
    allow_db_context: bool
    route_cache_ttl_s: int
    provider_cache_ttl_s: int
    request_timeout_s: float
    provider_connect_timeout_s: float
    provider_max_concurrency: int
    variant_workers: int
    micro_route_budget: int
    micro_min_eta_gain_s: int
    adaptive_variant_budget: int
    safety_variant_budget: int
    candidate_limit: int
    max_display_routes: int
    inflight_wait_s: float
    gzip_min_bytes: int
    gzip_level: int
    max_payload_kb: int
    app_version: str
    health_public: bool

    @classmethod
    def from_env(cls):
        node_id = str(os.environ.get("VAIGO_NODE_ID", "01")).strip()[:24] or "01"
        return cls(
            node_id=node_id,
            node_name=str(os.environ.get("VAIGO_NODE_NAME", f"VAIGO Node {node_id}")).strip()[:80] or f"VAIGO Node {node_id}",
            node_region=str(os.environ.get("VAIGO_NODE_REGION", "Render")).strip()[:80] or "Render",
            node_capacity=_int("VAIGO_NODE_CAPACITY", 4, 1, 32),
            central_api_secret=str(os.environ.get("CENTRAL_API_SECRET", "")).strip(),
            mapbox_token=str(os.environ.get("MAPBOX_ACCESS_TOKEN", os.environ.get("MAPBOX_TOKEN", ""))).strip(),
            mapbox_language=str(os.environ.get("MAPBOX_LANGUAGE", "pt-BR")).strip()[:20] or "pt-BR",
            database_url=str(os.environ.get("DATABASE_URL", "")).strip(),
            redis_url=str(os.environ.get("REDIS_URL", os.environ.get("VAIGO_REDIS_URL", ""))).strip(),
            allow_db_context=_bool("VAIGO_ALLOW_DB_CONTEXT", True),
            route_cache_ttl_s=_int("VAIGO_ROUTE_CACHE_TTL", 24, 3, 300),
            provider_cache_ttl_s=_int("VAIGO_PROVIDER_CACHE_TTL", 8, 1, 120),
            # Central defaults to a ~10 s route timeout. A worker should fail fast
            # enough for central failover rather than continue working after the
            # user request has already moved to another node.
            request_timeout_s=_float("VAIGO_PROVIDER_TIMEOUT", 7.5, 3.0, 20.0),
            provider_connect_timeout_s=_float("VAIGO_PROVIDER_CONNECT_TIMEOUT", 2.2, .5, 6.0),
            provider_max_concurrency=_int("VAIGO_PROVIDER_MAX_CONCURRENCY", 8, 2, 24),
            variant_workers=_int("VAIGO_VARIANT_WORKERS", 6, 2, 16),
            micro_route_budget=_int("VAIGO_MICRO_ROUTE_BUDGET", 4, 0, 8),
            micro_min_eta_gain_s=_int("VAIGO_MICRO_MIN_ETA_GAIN_SECONDS", 20, 0, 180),
            adaptive_variant_budget=_int("VAIGO_ADAPTIVE_VARIANT_BUDGET", 2, 0, 4),
            safety_variant_budget=_int("VAIGO_SAFETY_VARIANT_BUDGET", 2, 0, 5),
            candidate_limit=_int("VAIGO_CANDIDATE_LIMIT", 14, 6, 20),
            max_display_routes=_int("VAIGO_MAX_DISPLAY_ROUTES", 6, 1, 8),
            inflight_wait_s=_float("VAIGO_INFLIGHT_WAIT", 7.0, 1.0, 15.0),
            gzip_min_bytes=_int("VAIGO_GZIP_MIN_BYTES", 1800, 512, 65536),
            gzip_level=_int("VAIGO_GZIP_LEVEL", 3, 1, 6),
            max_payload_kb=_int("VAIGO_MAX_PAYLOAD_KB", 3072, 256, 8192),
            app_version=str(os.environ.get("VAIGO_NODE_VERSION", "node-v4")).strip()[:40] or "node-v4",
            health_public=_bool("VAIGO_HEALTH_PUBLIC", True),
        )

    def public_dict(self):
        return {
            "node_id": self.node_id,
            "node_name": self.node_name,
            "region": self.node_region,
            "capacity": self.node_capacity,
            "version": self.app_version,
            "mapbox_configured": bool(self.mapbox_token),
            "database_context": bool(self.database_url and self.allow_db_context),
            "shared_cache_configured": bool(self.redis_url),
            "route_cache_ttl_s": self.route_cache_ttl_s,
            "provider_cache_ttl_s": self.provider_cache_ttl_s,
            "provider_timeout_s": self.request_timeout_s,
            "provider_connect_timeout_s": self.provider_connect_timeout_s,
            "provider_max_concurrency": self.provider_max_concurrency,
            "variant_workers": self.variant_workers,
            "micro_route_budget": self.micro_route_budget,
            "micro_min_eta_gain_s": self.micro_min_eta_gain_s,
            "adaptive_variant_budget": self.adaptive_variant_budget,
            "safety_variant_budget": self.safety_variant_budget,
            "candidate_limit": self.candidate_limit,
            "max_display_routes": self.max_display_routes,
            "gzip_enabled": self.gzip_min_bytes > 0,
        }
