import hashlib
import math


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def point_to_segment_distance_m(lat, lon, lon1, lat1, lon2, lat2):
    # Small-area equirectangular projection is sufficient for corridor proximity checks.
    lat0 = math.radians((float(lat1) + float(lat2) + float(lat)) / 3.0)
    sx = 111320.0 * max(0.15, math.cos(lat0))
    sy = 110540.0
    px, py = (float(lon) - float(lon1)) * sx, (float(lat) - float(lat1)) * sy
    bx, by = (float(lon2) - float(lon1)) * sx, (float(lat2) - float(lat1)) * sy
    denom = bx * bx + by * by
    if denom <= 1e-9:
        return math.hypot(px, py)
    t = clamp((px * bx + py * by) / denom, 0.0, 1.0)
    return math.hypot(px - bx * t, py - by * t)


class PreparedGeometry:
    """Projected route geometry optimized for many point-to-route checks.

    Safety, admin zones and live-flow scoring may query the same route hundreds
    of times. Preparing the route once avoids repeated trigonometry and parsing.
    The route itself is never simplified in the response; this is only an
    internal proximity index.
    """

    __slots__ = (
        "lon0", "lat0", "sx", "sy", "segments",
        "min_lat", "max_lat", "min_lon", "max_lon", "points_count",
    )

    def __init__(self, coords, max_segments=900):
        valid = []
        for c in coords or []:
            try:
                lon, lat = float(c[0]), float(c[1])
            except Exception:
                continue
            if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
                valid.append((lon, lat))
        self.points_count = len(valid)
        if not valid:
            self.lon0 = self.lat0 = 0.0
            self.sx = 111320.0
            self.sy = 110540.0
            self.segments = ()
            self.min_lat = self.max_lat = self.min_lon = self.max_lon = 0.0
            return
        self.min_lon = min(x[0] for x in valid)
        self.max_lon = max(x[0] for x in valid)
        self.min_lat = min(x[1] for x in valid)
        self.max_lat = max(x[1] for x in valid)
        self.lon0 = (self.min_lon + self.max_lon) / 2.0
        self.lat0 = (self.min_lat + self.max_lat) / 2.0
        self.sx = 111320.0 * max(0.15, math.cos(math.radians(self.lat0)))
        self.sy = 110540.0
        if len(valid) == 1:
            self.segments = ()
            return
        max_segments = max(64, int(max_segments))
        stride = max(1, math.ceil((len(valid) - 1) / max_segments))
        sampled = valid[::stride]
        if sampled[-1] != valid[-1]:
            sampled.append(valid[-1])
        projected = [((lon - self.lon0) * self.sx, (lat - self.lat0) * self.sy) for lon, lat in sampled]
        segments = []
        for i in range(len(projected) - 1):
            x1, y1 = projected[i]
            x2, y2 = projected[i + 1]
            dx, dy = x2 - x1, y2 - y1
            denom = dx * dx + dy * dy
            segments.append((x1, y1, dx, dy, denom, min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)))
        self.segments = tuple(segments)

    def near_bbox(self, lat, lon, margin_m=0.0):
        if self.points_count == 0:
            return False
        margin_m = max(0.0, float(margin_m or 0))
        lat_pad = margin_m / self.sy
        lon_pad = margin_m / self.sx
        return (
            self.min_lat - lat_pad <= float(lat) <= self.max_lat + lat_pad
            and self.min_lon - lon_pad <= float(lon) <= self.max_lon + lon_pad
        )

    def distance_m(self, lat, lon):
        if self.points_count == 0:
            return 10**9
        px = (float(lon) - self.lon0) * self.sx
        py = (float(lat) - self.lat0) * self.sy
        if not self.segments:
            return math.hypot(px, py)
        best2 = float("inf")
        for x1, y1, dx, dy, denom, minx, maxx, miny, maxy in self.segments:
            # Cheap rectangle lower bound skips distant segments once a nearby
            # segment has already been found.
            if best2 < float("inf"):
                qx = minx if px < minx else maxx if px > maxx else px
                qy = miny if py < miny else maxy if py > maxy else py
                if (px - qx) ** 2 + (py - qy) ** 2 >= best2:
                    continue
            if denom <= 1e-9:
                d2 = (px - x1) ** 2 + (py - y1) ** 2
            else:
                t = clamp(((px - x1) * dx + (py - y1) * dy) / denom, 0.0, 1.0)
                ex = px - (x1 + dx * t)
                ey = py - (y1 + dy * t)
                d2 = ex * ex + ey * ey
            if d2 < best2:
                best2 = d2
                if best2 <= .25:
                    return math.sqrt(best2)
        return math.sqrt(best2) if best2 < float("inf") else 10**9


def prepare_route_geometry(route, max_segments=900):
    if not isinstance(route, dict):
        return PreparedGeometry([], max_segments=max_segments)
    cached = route.get("_prepared_geometry_v4")
    if isinstance(cached, PreparedGeometry):
        return cached
    coords = ((route.get("geometry") or {}).get("coordinates") or [])
    prepared = PreparedGeometry(coords, max_segments=max_segments)
    route["_prepared_geometry_v4"] = prepared
    return prepared


def min_distance_to_geometry_m(lat, lon, coords):
    if not coords:
        return 10**9
    prepared = PreparedGeometry(coords, max_segments=900)
    return prepared.distance_m(lat, lon)


def sanitize_bearing(value):
    try:
        v = float(value)
    except Exception:
        return None
    if not math.isfinite(v):
        return None
    return round(v % 360.0, 1)


def sanitize_speed(value):
    try:
        v = float(value)
    except Exception:
        return None
    if not math.isfinite(v) or v < 0:
        return None
    return round(min(v, 70.0), 2)


def bearing_delta_deg(a, b):
    return abs(((float(a) - float(b) + 180.0) % 360.0) - 180.0)


def route_initial_bearing(route):
    coords = ((route or {}).get("geometry") or {}).get("coordinates") or []
    valid = []
    for c in coords[:100]:
        try:
            lon, lat = float(c[0]), float(c[1])
        except Exception:
            continue
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            valid.append((lon, lat))
    if len(valid) < 2:
        return None
    a = valid[0]
    for b in valid[1:]:
        if haversine_m(a[1], a[0], b[1], b[0]) < 14:
            continue
        y = math.sin(math.radians(b[0] - a[0])) * math.cos(math.radians(b[1]))
        x = math.cos(math.radians(a[1])) * math.sin(math.radians(b[1])) - math.sin(math.radians(a[1])) * math.cos(math.radians(b[1])) * math.cos(math.radians(b[0] - a[0]))
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return None


def apply_start_direction_guard(routes, bearing=None, speed=None, moving_threshold=1.2):
    routes = list(routes or [])
    b = sanitize_bearing(bearing)
    s = sanitize_speed(speed)
    if b is None or s is None or s < moving_threshold or not routes:
        return routes, {"active": False, "rejected": 0, "bearing": b, "speed_mps": s}
    accepted = []
    rejected = 0
    for route in routes:
        rb = route_initial_bearing(route)
        delta = bearing_delta_deg(rb, b) if rb is not None else None
        route["_start_direction_bearing"] = round(rb, 1) if rb is not None else None
        route["_start_direction_delta"] = round(delta, 1) if delta is not None else None
        ok = delta is None or delta <= 112.0
        route["_start_direction_ok"] = ok
        if ok:
            accepted.append(route)
        else:
            rejected += 1
    if not accepted:
        return routes, {"active": True, "rejected": 0, "bearing": b, "speed_mps": s, "fallback": "provider-set"}
    return accepted, {"active": True, "rejected": rejected, "bearing": b, "speed_mps": s}


def route_signature(route):
    if isinstance(route, dict):
        cached = route.get("_route_signature_v4")
        if isinstance(cached, str):
            return cached
    coords = ((route or {}).get("geometry") or {}).get("coordinates") or []
    if not coords:
        return ""
    step = max(1, len(coords) // 18)
    sample = coords[::step][:20]
    raw = "|".join(f"{float(c[0]):.4f},{float(c[1]):.4f}" for c in sample if len(c) >= 2)
    sig = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]
    if isinstance(route, dict):
        route["_route_signature_v4"] = sig
    return sig


def route_spatial_cells(route, cell_m=72):
    key = int(round(float(cell_m)))
    if isinstance(route, dict):
        cache = route.get("_route_cells_v4")
        if isinstance(cache, dict) and key in cache:
            return cache[key]
    coords = ((route or {}).get("geometry") or {}).get("coordinates") or []
    valid = []
    for c in coords:
        try:
            valid.append((float(c[0]), float(c[1])))
        except Exception:
            pass
    if len(valid) < 2:
        return set()
    avg_lat = sum(c[1] for c in valid) / len(valid)
    lat_step = max(.00012, float(cell_m) / 110540.0)
    lon_step = max(.00012, float(cell_m) / (111320.0 * max(.22, math.cos(math.radians(avg_lat)))))
    stride = max(1, len(valid) // 700)
    cells = {(int(round(lat / lat_step)), int(round(lon / lon_step))) for lon, lat in valid[::stride]}
    lon, lat = valid[-1]
    cells.add((int(round(lat / lat_step)), int(round(lon / lon_step))))
    if isinstance(route, dict):
        cache = route.get("_route_cells_v4")
        if not isinstance(cache, dict):
            cache = {}
            route["_route_cells_v4"] = cache
        cache[key] = cells
    return cells


def route_overlap_ratio(a, b):
    ca, cb = route_spatial_cells(a), route_spatial_cells(b)
    if not ca or not cb:
        return 1.0 if route_signature(a) == route_signature(b) else 0.0
    return len(ca & cb) / max(1, min(len(ca), len(cb)))


def select_diverse_routes(routes, max_routes=6, max_overlap=.93, sort_key=None):
    ordered = list(routes or [])
    if sort_key:
        ordered.sort(key=sort_key)
    out = []
    for route in ordered:
        if not ((route or {}).get("geometry") or {}).get("coordinates"):
            continue
        if any(route_overlap_ratio(route, other) >= max_overlap for other in out):
            continue
        out.append(route)
        if len(out) >= max_routes:
            break
    return out or ordered[:1]
