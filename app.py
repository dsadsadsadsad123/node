import copy
import gzip
import hashlib
import hmac
import json
import os
import time

from flask import Flask, jsonify, request

from cache_backend import SharedCache
from config import Settings
from context_store import ContextStore
from mapbox_provider import MapboxProvider
from route_engine import RouteEngine, normalize_job
from runtime import CapacityGate, InflightCoordinator, Metrics, system_snapshot


settings = Settings.from_env()
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = settings.max_payload_kb * 1024
try:
    app.json.sort_keys = False
    app.json.compact = True
except Exception:
    pass

provider = MapboxProvider(
    settings.mapbox_token,
    language=settings.mapbox_language,
    timeout_s=settings.request_timeout_s,
    connect_timeout_s=settings.provider_connect_timeout_s,
    provider_cache_ttl_s=settings.provider_cache_ttl_s,
    max_concurrency=settings.provider_max_concurrency,
)
context_store = ContextStore(settings.database_url, enabled=settings.allow_db_context)
cache = SharedCache(settings.redis_url, default_ttl=settings.route_cache_ttl_s)
engine = RouteEngine(
    provider,
    max_display_routes=settings.max_display_routes,
    micro_route_budget=settings.micro_route_budget,
    micro_min_eta_gain_s=settings.micro_min_eta_gain_s,
    adaptive_variant_budget=settings.adaptive_variant_budget,
    safety_variant_budget=settings.safety_variant_budget,
    candidate_limit=settings.candidate_limit,
    variant_workers=settings.variant_workers,
)
gate = CapacityGate(settings.node_capacity)
metrics = Metrics()
inflight = InflightCoordinator(max_entries=256)


def _provided_secret():
    auth = str(request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(request.headers.get("X-VAIGO-Secret") or "").strip()


def _authorized():
    if not settings.central_api_secret:
        # Fail closed in production; only explicit localhost smoke tests may run
        # without the shared Central secret.
        return request.remote_addr in {"127.0.0.1", "::1", None} and os.environ.get("VAIGO_ALLOW_INSECURE_LOCAL", "0").lower() in {"1", "true", "yes", "on"}
    supplied = _provided_secret()
    return bool(supplied and hmac.compare_digest(supplied, settings.central_api_secret))


def _require_auth():
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized_node_request"}), 401
    return None


def _empty_context(source="none"):
    return {"reports": [], "risk_zones": [], "flow_samples": [], "source": source}


def _node_snapshot(include_system=True):
    mapbox_ready = provider.ready()
    ready_reason = "ready" if mapbox_ready else "MAPBOX_ACCESS_TOKEN não configurado"
    snap = {
        "node_id": settings.node_id,
        "node_name": settings.node_name,
        "region": settings.node_region,
        "version": settings.app_version,
        "status": "online" if mapbox_ready else "degraded",
        "healthy": mapbox_ready,
        "ready_reason": ready_reason,
        **gate.snapshot(),
        **metrics.snapshot(),
        **inflight.snapshot(),
        **provider.snapshot(),
        "cache_mode": cache.mode,
        "database_context": context_store.ready(),
        "mapbox_ready": mapbox_ready,
    }
    if include_system:
        snap.update(system_snapshot())
    return snap


def _context_for(job, raw_payload, lightweight=False):
    # Fastest is intentionally ETA-only in the Central. Skipping reports/zones
    # here avoids DB/context work and improves cache reuse for that hot path.
    if job.get("mode") == "fastest" and not raw_payload.get("modes"):
        return _empty_context("eta-only")
    supplied = raw_payload.get("context")
    if isinstance(supplied, dict):
        return {
            "reports": list(supplied.get("reports") or [])[:500],
            "risk_zones": list(supplied.get("risk_zones") or [])[:400],
            "flow_samples": list(supplied.get("flow_samples") or [])[:400],
            "source": str(supplied.get("source") or "central-payload")[:80],
        }
    return context_store.load(job["start"], job["end"], lightweight=lightweight)


def _context_fingerprint(context):
    payload = {
        "reports": [
            (x.get("id"), x.get("confirmations"), x.get("severity"), x.get("created_at"))
            for x in (context.get("reports") or [])[:120]
        ],
        "zones": [
            (x.get("id"), x.get("updated_at"), x.get("active"), x.get("block_routes"), x.get("danger_level"), x.get("confidence"))
            for x in (context.get("risk_zones") or [])[:120]
        ],
        "flow": [
            (x.get("cell_lat"), x.get("cell_lon"), x.get("avg_speed"), x.get("sources"), x.get("updated_at"))
            for x in (context.get("flow_samples") or [])[:80]
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()[:14]


def _cache_key(job, context=None):
    mode = job["mode"]
    normalized = {
        "start": {"lat": round(job["start"]["lat"], 5), "lon": round(job["start"]["lon"], 5)},
        "end": {"lat": round(job["end"]["lat"], 5), "lon": round(job["end"]["lon"], 5)},
        "profile": job["profile"],
        "mode": mode,
        "depart_at": job.get("depart_at"),
        "heading": None if job.get("heading") is None else round(float(job.get("heading")), -1),
        "speed_bucket": None if job.get("speed") is None else round(float(job.get("speed")), 0),
        "reroute": bool(job.get("reroute")),
        "adaptive": bool(job.get("adaptive", True)),
        "preferences": job.get("preferences"),
    }
    if mode == "fastest":
        # ETA-only route selection must not fragment the cache by safety profile
        # or by reports that are deliberately ignored for this mode.
        normalized["context_fingerprint"] = "eta-only-v4"
    else:
        normalized.update({
            "professional_driver": bool(job.get("professional_driver")),
            "local_hour": job.get("local_hour"),
            "night_active": bool(job.get("night_active")),
            "safety_bias": round(float(job.get("safety_bias") or 68), 0),
            "traffic_bias": round(float(job.get("traffic_bias") or 62), 0),
            "context_fingerprint": _context_fingerprint(context or _empty_context()),
        })
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return "vaigo:route:" + hashlib.sha256(raw.encode()).hexdigest()


def _decorate_result(raw_result, job, cache_hit=False, bundle=False, coalesced=False):
    body = dict(raw_result or {})
    body["request_id"] = job.get("request_id")
    body["cache"] = {
        "hit": bool(cache_hit),
        "mode": cache.mode,
        **({"bundle": True} if bundle else {}),
        **({"coalesced": True} if coalesced else {}),
    }
    body["node"] = _node_snapshot(include_system=False)
    return body


def _cacheable_result(result):
    # Keep transient node/cache telemetry out of cached route payloads.
    return {k: v for k, v in dict(result or {}).items() if k not in {"node", "cache"}}


def _calculate_payload(raw_payload, kind="route"):
    started = time.perf_counter()
    try:
        job = normalize_job(raw_payload)
    except Exception as exc:
        metrics.record((time.perf_counter() - started) * 1000, False, kind)
        return {"ok": False, "error": "invalid_route_job", "detail": str(exc)[:220]}, 400

    context = _empty_context("eta-only") if job["mode"] == "fastest" else _context_for(job, raw_payload, lightweight=False)
    key = _cache_key(job, context)
    cached = cache.get(key)
    if cached is not None:
        metrics.cache_hit()
        body = _decorate_result(cached, job, cache_hit=True)
        metrics.record((time.perf_counter() - started) * 1000, True, kind)
        return body, 200
    metrics.cache_miss()

    owner, shared_job = inflight.claim(key)
    if not owner:
        metrics.coalesced()
        shared = inflight.wait(shared_job, settings.inflight_wait_s)
        if shared is None:
            metrics.inflight_timeout()
            metrics.record((time.perf_counter() - started) * 1000, False, kind)
            return {
                "ok": False,
                "error": "inflight_wait_timeout",
                "retryable": True,
                "request_id": job["request_id"],
                "node": _node_snapshot(include_system=False),
            }, 503
        body, status = shared
        if status == 200:
            # Prefer the cache copy because it excludes the owner's request_id.
            cached = cache.get(key)
            if cached is not None:
                body = _decorate_result(cached, job, cache_hit=True, coalesced=True)
            else:
                body = copy.deepcopy(body)
                body["request_id"] = job["request_id"]
        else:
            body = copy.deepcopy(body)
            body["request_id"] = job["request_id"]
        metrics.record((time.perf_counter() - started) * 1000, status == 200, kind)
        return body, status

    acquired = False
    response = None
    try:
        if not gate.try_acquire():
            snap = _node_snapshot(include_system=False)
            response = ({
                "ok": False,
                "error": "node_capacity_reached",
                "retryable": True,
                "retry_after_ms": 500,
                "node": snap,
                "request_id": job["request_id"],
            }, 429)
            metrics.record((time.perf_counter() - started) * 1000, False, kind)
            return response
        acquired = True
        result = engine.calculate(job, context)
        cache.set(key, _cacheable_result(result), settings.route_cache_ttl_s)
        body = _decorate_result(result, job, cache_hit=False)
        response = (body, 200)
        metrics.record((time.perf_counter() - started) * 1000, True, kind)
        return response
    except Exception as exc:
        response = ({
            "ok": False,
            "error": "route_calculation_failed",
            "detail": str(exc)[:320],
            "retryable": True,
            "request_id": job.get("request_id"),
            "node": _node_snapshot(include_system=False),
        }, 502)
        metrics.record((time.perf_counter() - started) * 1000, False, kind)
        return response
    finally:
        if acquired:
            gate.release()
        if response is not None:
            inflight.finish(key, response)


def _mode_context(job, payload, mode, loaded):
    if mode == "fastest":
        return _empty_context("eta-only")
    if loaded[0] is None:
        probe = dict(job)
        probe["mode"] = mode
        loaded[0] = _context_for(probe, payload, lightweight=False)
    return loaded[0]


@app.after_request
def _response_optimizations(response):
    response.headers["X-VAIGO-Node-ID"] = settings.node_id
    response.headers["X-VAIGO-Node-Version"] = settings.app_version
    # requests (used by the Central) transparently decompresses gzip. Compressing
    # route JSON removes a large amount of repeated GeoJSON from the wire while
    # keeping the exact API contract unchanged.
    try:
        accepts = str(request.headers.get("Accept-Encoding") or "").lower()
        if (
            "gzip" in accepts
            and response.status_code not in {204, 304}
            and not response.headers.get("Content-Encoding")
            and response.mimetype == "application/json"
            and len(response.get_data()) >= settings.gzip_min_bytes
        ):
            compressed = gzip.compress(response.get_data(), compresslevel=settings.gzip_level)
            if len(compressed) + 80 < len(response.get_data()):
                response.set_data(compressed)
                response.headers["Content-Encoding"] = "gzip"
                response.headers["Vary"] = "Accept-Encoding"
                response.headers["Content-Length"] = str(len(compressed))
    except Exception:
        pass
    return response


@app.get("/")
def root():
    return jsonify({
        "service": "VAIGO Route Node",
        "node": settings.node_name,
        "version": settings.app_version,
        "engine": "vaigo-route-node-v4",
        "endpoints": [
            "/livez", "/healthz", "/readyz", "/v1/route/calculate",
            "/v1/route/precalculate", "/v1/metrics", "/v1/config",
        ],
    })


@app.get("/livez")
def livez():
    return jsonify({"ok": True, "alive": True, "node_id": settings.node_id, "version": settings.app_version}), 200


@app.get("/healthz")
def healthz():
    if not settings.health_public:
        denied = _require_auth()
        if denied:
            return denied
    return jsonify(_node_snapshot()), 200


@app.get("/readyz")
def readyz():
    if not settings.health_public:
        denied = _require_auth()
        if denied:
            return denied
    ok = provider.ready()
    return jsonify({
        "ready": ok,
        "mapbox_ready": ok,
        "node_id": settings.node_id,
        "version": settings.app_version,
        **gate.snapshot(),
    }), 200 if ok else 503


@app.get("/v1/metrics")
def node_metrics():
    denied = _require_auth()
    if denied:
        return denied
    return jsonify(_node_snapshot())


@app.get("/v1/config")
def node_config():
    denied = _require_auth()
    if denied:
        return denied
    return jsonify(settings.public_dict())


@app.post("/v1/route/calculate")
def calculate_route():
    denied = _require_auth()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    body, status = _calculate_payload(payload, "route")
    response = jsonify(body)
    if status == 429:
        response.headers["Retry-After"] = "1"
    return response, status


@app.post("/v1/route/precalculate")
def precalculate_routes():
    """Calculate/cache several modes from one shared provider candidate wave."""
    denied = _require_auth()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    modes = payload.get("modes") or ["safest", "fastest"]
    modes = [str(x).lower() for x in modes if str(x).lower() in {"safest", "fastest", "smart", "quietest"}][:4]
    modes = list(dict.fromkeys(modes))
    if not modes:
        return jsonify({"ok": False, "error": "no_valid_modes"}), 400

    started = time.perf_counter()
    try:
        seed = dict(payload)
        requested = str(payload.get("mode") or "").strip().lower()
        seed["mode"] = requested if requested in modes else modes[0]
        job = normalize_job(seed)
    except Exception as exc:
        metrics.record((time.perf_counter() - started) * 1000, False, "precalc-bundle")
        return jsonify({"ok": False, "error": "invalid_route_job", "detail": str(exc)[:220]}), 400

    results = {}
    missing = []
    context_loaded = [None]
    mode_keys = {}
    for mode in modes:
        one = dict(job)
        one["mode"] = mode
        context = _mode_context(one, payload, mode, context_loaded)
        key = _cache_key(one, context)
        mode_keys[mode] = key
        cached = cache.get(key)
        if cached is not None:
            metrics.cache_hit()
            results[mode] = _decorate_result(cached, job, cache_hit=True, bundle=True)
            results[mode]["mode"] = mode
        else:
            metrics.cache_miss()
            missing.append(mode)

    if missing:
        if not gate.try_acquire():
            # Prefetch is opportunistic. If the mode the Central actually needs
            # is already cached, serve it immediately with HTTP 200 instead of
            # forcing a node failover just because the *other* warm-up modes
            # could not consume a heavy slot.
            requested_ready = job.get("mode") in results
            metrics.record((time.perf_counter() - started) * 1000, requested_ready, "precalc-bundle")
            status = 200 if requested_ready else (207 if results else 429)
            payload_out = {
                "ok": bool(results),
                "results": results,
                "shared_pool": True,
                "partial_prefetch": True,
                "retryable": not requested_ready,
                "node": _node_snapshot(include_system=False),
                "request_id": job["request_id"],
            }
            if not requested_ready:
                payload_out.update({"error": "node_capacity_reached", "retry_after_ms": 500})
            response = jsonify(payload_out)
            if status == 429:
                response.headers["Retry-After"] = "1"
            return response, status
        try:
            context = context_loaded[0]
            if context is None:
                # All missing modes are fastest.
                context = _empty_context("eta-only")
            computed = engine.calculate_bundle(job, context, missing)
            for mode, raw_body in computed.items():
                one = dict(job)
                one["mode"] = mode
                cache.set(mode_keys[mode], _cacheable_result(raw_body), settings.route_cache_ttl_s)
                body = _decorate_result(raw_body, job, cache_hit=False, bundle=True)
                body["mode"] = mode
                results[mode] = body
        except Exception as exc:
            requested_ready = job.get("mode") in results
            metrics.record((time.perf_counter() - started) * 1000, requested_ready, "precalc-bundle")
            payload_out = {
                "ok": bool(results),
                "results": results,
                "shared_pool": True,
                "partial_prefetch": True,
                "node": _node_snapshot(include_system=False),
                "request_id": job["request_id"],
            }
            if requested_ready:
                payload_out["prefetch_warning"] = "secondary_modes_failed"
                return jsonify(payload_out), 200
            payload_out.update({
                "error": "route_calculation_failed",
                "detail": str(exc)[:320],
                "retryable": True,
            })
            return jsonify(payload_out), 207 if results else 502
        finally:
            gate.release()

    metrics.record((time.perf_counter() - started) * 1000, True, "precalc-bundle")
    ordered_results = {m: results[m] for m in modes if m in results}
    return jsonify({
        "ok": bool(ordered_results),
        "results": ordered_results,
        "shared_pool": True,
        "processing_ms": round((time.perf_counter() - started) * 1000, 1),
        "node": _node_snapshot(include_system=False),
        "request_id": job["request_id"],
    }), 200


@app.post("/v1/cache/invalidate")
def invalidate_route_cache():
    denied = _require_auth()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    key = str(payload.get("cache_key") or "")
    if key.startswith("vaigo:route:"):
        cache.delete(key)
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "invalid_cache_key"}), 400


@app.errorhandler(404)
def not_found(_):
    return jsonify({"ok": False, "error": "not_found"}), 404


@app.errorhandler(413)
def too_large(_):
    return jsonify({"ok": False, "error": "payload_too_large", "max_kb": settings.max_payload_kb}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
