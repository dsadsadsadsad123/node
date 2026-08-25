import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from geo import (
    apply_start_direction_guard,
    clamp,
    haversine_m,
    prepare_route_geometry,
    route_overlap_ratio,
    route_signature,
    select_diverse_routes,
    sanitize_bearing,
    sanitize_speed,
)


CATEGORY_META = {
    "robbery": {"label": "Roubo/furto", "weight": 1.35, "quiet": .15},
    "harassment": {"label": "Assédio/importunação", "weight": 1.30, "quiet": .10},
    "poor_lighting": {"label": "Iluminação ruim", "weight": 1.05, "quiet": .05},
    "accident": {"label": "Acidente/risco viário", "weight": 1.15, "quiet": .20},
    "traffic": {"label": "Trânsito parado", "weight": .72, "quiet": .45},
    "road_block": {"label": "Via bloqueada", "weight": 1.05, "quiet": .20},
    "blitz": {"label": "Fiscalização", "weight": .18, "quiet": .20},
    "road_hazard": {"label": "Perigo na via", "weight": .95, "quiet": .25},
    "flood": {"label": "Alagamento", "weight": 1.25, "quiet": .10},
    "construction": {"label": "Obra/bloqueio", "weight": .90, "quiet": .30},
    "crowd": {"label": "Aglomeração/evento", "weight": .45, "quiet": 1.40},
    "other": {"label": "Outro alerta", "weight": .75, "quiet": .30},
}


def _dict(row):
    return dict(row or {})


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _safety_level(score):
    score = float(score or 0)
    if score >= 86: return 5
    if score >= 70: return 4
    if score >= 50: return 3
    if score >= 32: return 2
    if score >= 15: return 1
    return 0


def _safety_label(level):
    return {5:"Muito favorável",4:"Favorável",3:"Atenção",2:"Cautela elevada",1:"Risco alto",0:"Evite se possível"}.get(int(level), "Atenção")


def _zone_active(zone, local_hour):
    if local_hour is None or zone.get("start_hour") is None or zone.get("end_hour") is None:
        return True
    try:
        start_h, end_h = int(zone["start_hour"]), int(zone["end_hour"])
    except Exception:
        return True
    if start_h == end_h:
        return True
    if start_h < end_h:
        return start_h <= local_hour < end_h
    return local_hour >= start_h or local_hour < end_h


def _risk_profile_factor(category, profile, local_hour=None):
    personal = category in {"robbery", "harassment", "poor_lighting"}
    road = category in {"accident", "flood", "construction", "road_block", "road_hazard"}
    if profile == "walking":
        factor = 1.20 if personal else .92 if road else 1.0
    elif profile == "cycling":
        factor = 1.05 if personal else 1.12 if road else 1.0
    elif profile == "motorcycle":
        factor = .88 if personal else 1.38 if road else 1.05
    else:
        factor = .78 if personal else 1.22 if road else 1.0
    if category == "poor_lighting" and local_hour is not None and (local_hour >= 18 or local_hour <= 6):
        factor *= 1.18 if profile == "motorcycle" else (1.35 if profile != "driving" else 1.12)
    return factor


def _risk_radius(category, severity, profile):
    base = {
        "robbery":460,"harassment":360,"poor_lighting":310,"accident":245,"flood":330,
        "construction":210,"road_block":230,"road_hazard":220,"crowd":190,"other":240,
    }.get(category, 240)
    base *= .92 + clamp(float(severity), 1, 5) * .035
    if profile == "driving" and category in {"accident","flood","construction","road_block","road_hazard"}:
        base *= 1.12
    elif profile == "motorcycle" and category in {"accident","flood","construction","road_block","road_hazard"}:
        base *= 1.20
    return clamp(base, 150, 620)


def route_risk_metrics(route, reports, risk_zones, local_hour=None, profile="driving"):
    coords = ((route or {}).get("geometry") or {}).get("coordinates") or []
    prepared = prepare_route_geometry(route)
    route_length_m = max(float((route or {}).get("distance") or 0), 1.0)
    distance_km = route_length_m / 1000.0
    now = datetime.now(timezone.utc)
    nearby, zone_hits, risk_factors = [], [], []
    level_cap = 5
    quiet_penalty = weighted_exposure_m = zone_exposure_m = hotspot_risk = evidence_strength = 0.0
    high_signal_count = confirmations_total = 0

    for raw in reports or []:
        report = _dict(raw)
        category = str(report.get("category") or "other")
        if category == "blitz":
            # Enforcement is not used as a route-avoidance signal.
            continue
        try:
            severity_i = int(clamp(int(report.get("severity") or 3), 1, 5))
            lat, lon = float(report["latitude"]), float(report["longitude"])
        except Exception:
            continue
        radius = _risk_radius(category, severity_i, profile)
        if not prepared.near_bbox(lat, lon, radius + 260):
            continue
        d = prepared.distance_m(lat, lon)
        if d > radius + 260:
            continue
        created = _parse_iso(report.get("created_at")) or now
        age_h = max(0.0, (now - created).total_seconds() / 3600.0)
        freshness = math.exp(-age_h / (24 * 7.0))
        proximity = max(0.0, 1.0 - d / max(radius, 1)) ** 1.55
        confirmations = min(int(report.get("confirmations") or 0), 12)
        confirmations_total += confirmations
        meta = CATEGORY_META.get(category, CATEGORY_META["other"])
        strength = (severity_i / 5.0) * float(meta["weight"]) * freshness * (.90 + min(.55, confirmations * .055)) * _risk_profile_factor(category, profile, local_hour)
        event_risk = clamp(58.0 * strength * proximity, 0, 100)
        hotspot_risk = max(hotspot_risk, event_risk)
        if event_risk >= 24:
            high_signal_count += 1
        if d < radius:
            chord = 2.0 * math.sqrt(max(0.0, radius * radius - d * d))
            weighted_exposure_m += chord * clamp(.30 + strength * .70, .18, 1.70)
        quiet_penalty += 13.0 * (severity_i / 5.0) * float(meta["quiet"]) * freshness * max(.08, proximity)
        evidence_strength += clamp(event_risk / 42.0, .05, 1.5)
        if category == "robbery" and d <= 300 and severity_i >= 4 and confirmations >= 2 and freshness >= .20:
            level_cap = min(level_cap, 3)
            risk_factors.append("Relatos recentes e confirmados próximos ao corredor")
        if category == "flood" and d <= 180 and severity_i >= 4:
            risk_factors.append("Alagamento relevante reportado próximo da rota")
        if d <= min(430, radius + 80):
            nearby.append({
                "id": report.get("id"), "category": category, "category_label": meta["label"],
                "title": str(report.get("title") or meta["label"])[:160], "severity": severity_i,
                "distance_to_route_m": round(d), "created_at": report.get("created_at"),
                "lat": lat, "lon": lon, "confirmations": confirmations, "risk_strength": round(event_risk, 1),
            })

    zone_hotspot = 0.0
    for raw in risk_zones or []:
        zone = _dict(raw)
        if not _zone_active(zone, local_hour):
            continue
        try:
            lat, lon = float(zone["latitude"]), float(zone["longitude"])
            radius = clamp(float(zone.get("radius_m") or 350), 80, 5000)
        except Exception:
            continue
        if not prepared.near_bbox(lat, lon, radius + 420):
            continue
        d = prepared.distance_m(lat, lon)
        if d > radius + 420:
            continue
        confidence = clamp(float(zone.get("confidence") or .75), 0, 1)
        if d <= radius:
            cap = int(clamp(int(zone.get("level_cap") if zone.get("level_cap") is not None else 3), 0, 5))
            level_cap = min(level_cap, cap)
            proximity = max(.10, 1.0 - d / max(radius, 1))
            zone_event_risk = clamp(72.0 * confidence * (proximity ** 1.25), 0, 100)
            zone_hotspot = max(zone_hotspot, zone_event_risk)
            chord = 2.0 * math.sqrt(max(0.0, radius * radius - d * d))
            zone_exposure_m += chord * clamp(confidence, .2, 1.0)
            evidence_strength += .75 + confidence
            zone_hits.append({
                "id": zone.get("id"), "name": str(zone.get("name") or "Zona de atenção")[:140],
                "risk_type": zone.get("risk_type"), "level_cap": cap, "distance_to_route_m": round(d),
                "source": zone.get("source"), "lat": lat, "lon": lon, "radius_m": round(radius),
                "confidence": round(confidence, 2), "block_routes": bool(zone.get("block_routes")),
            })
            risk_factors.append(f"Zona de atenção verificada: {str(zone.get('name') or 'área')[:100]}")

    exposure_ratio = clamp(weighted_exposure_m / route_length_m, 0, 2.2)
    zone_ratio = clamp(zone_exposure_m / route_length_m, 0, 1.8)
    exposure_risk = clamp(exposure_ratio * 50.0, 0, 100)
    zone_risk = clamp(zone_ratio * 62.0, 0, 100)
    cluster_risk = clamp((high_signal_count / max(distance_km, .8)) * 11.0, 0, 55)
    hotspot_risk = max(hotspot_risk, zone_hotspot)
    observed_risk = clamp(exposure_risk * .46 + hotspot_risk * .34 + zone_risk * .14 + cluster_risk * .06, 0, 99)
    observed_safety = clamp(100 - observed_risk, 1, 100)
    evidence_density = evidence_strength / max(1.0, math.sqrt(max(distance_km, .6)))
    data_conf = clamp(30 + math.log1p(evidence_density) * 23 + min(15, confirmations_total * .85), 30, 93)
    blend = .28 + .72 * clamp((data_conf - 30) / 63, 0, 1)
    uncertainty_penalty = max(0, 58 - data_conf) * .09
    safety_score = clamp(58 + (observed_safety - 58) * blend - uncertainty_penalty, 1, 100)
    conservative = clamp(safety_score - max(0, 68 - data_conf) * .12, 1, 100)
    if data_conf < 42: level_cap = min(level_cap, 3)
    elif data_conf < 58: level_cap = min(level_cap, 4)
    level = min(_safety_level(safety_score), level_cap)
    return {
        "risk": round(observed_risk, 2), "observed_safety_score": round(observed_safety, 1),
        "safety_score": round(safety_score, 1), "safety_conservative_score": round(conservative, 1),
        "safety_level": int(level), "safety_level_label": _safety_label(level),
        "data_confidence": round(data_conf), "data_confidence_label": "alta" if data_conf >= 78 else "média" if data_conf >= 56 else "limitada",
        "uncertainty_penalty": round(uncertainty_penalty, 1), "risk_exposure_pct": round(clamp(exposure_ratio * 100, 0, 100), 1),
        "hotspot_risk": round(hotspot_risk, 1), "cluster_risk": round(cluster_risk, 1),
        "evidence_count": len(nearby) + len(zone_hits), "evidence_density": round(evidence_density, 3),
        "quiet_score": round(clamp(100 - quiet_penalty, 1, 100), 1),
        "nearby_alerts": sorted(nearby, key=lambda x: (-x["risk_strength"], x["distance_to_route_m"]))[:14],
        "risk_zones": sorted(zone_hits, key=lambda x: (x["distance_to_route_m"], -x["confidence"]))[:10],
        "risk_factors": list(dict.fromkeys(risk_factors))[:7], "safety_engine": "vaigo-safety-node-v1",
    }


def traffic_level(score):
    score = float(score or 0)
    if score < 28: return "Leve"
    if score < 52: return "Moderado"
    if score < 74: return "Intenso"
    return "Muito intenso"


def route_traffic_metrics(route):
    empty = {"traffic_score":0,"traffic_level":"Sem dados","congested_distance_km":0,"traffic_points":[],"traffic_segments":[],"severe_segments":0,"traffic_source":"mapbox-driving-traffic"}
    if not route:
        return dict(empty)
    geometry = ((route.get("geometry") or {}).get("coordinates") or [])
    if len(geometry) < 2:
        return dict(empty)
    weights = {"unknown":15,"low":15,"moderate":48,"heavy":76,"severe":94}
    samples, severe = [], []
    cursor = 0.0
    for leg in route.get("legs") or []:
        ann = leg.get("annotation") or {}
        nums, labels, distances = list(ann.get("congestion_numeric") or []), list(ann.get("congestion") or []), list(ann.get("distance") or [])
        n = max(len(nums), len(labels), len(distances))
        for i in range(n):
            d = max(.2, float(distances[i]) if i < len(distances) and distances[i] is not None else 1.0)
            label = str(labels[i]).lower() if i < len(labels) and labels[i] else "unknown"
            score = clamp(float(nums[i]) if i < len(nums) and nums[i] is not None else weights.get(label, 20), 0, 100)
            samples.append({"start":cursor,"end":cursor+d,"distance":d,"score":score,"label":label})
            if score >= 65 or label in {"heavy","severe"}: severe.append(len(samples)-1)
            cursor += d
    if not samples:
        return dict(empty)
    total_d = sum(x["distance"] for x in samples)
    score = sum(x["score"] * x["distance"] for x in samples) / max(total_d, 1)
    congested = sum(x["distance"] for x in samples if x["score"] >= 65 or x["label"] in {"heavy","severe"})
    geom_cum = [0.0]
    for i in range(1, len(geometry)):
        try: geom_cum.append(geom_cum[-1] + haversine_m(geometry[i-1][1], geometry[i-1][0], geometry[i][1], geometry[i][0]))
        except Exception: geom_cum.append(geom_cum[-1])
    geom_total = max(1.0, geom_cum[-1])
    def geom_index(provider_m):
        target = clamp(provider_m / max(total_d,1),0,1) * geom_total
        lo, hi = 0, len(geom_cum)-1
        while lo < hi:
            mid=(lo+hi)//2
            if geom_cum[mid] < target: lo=mid+1
            else: hi=mid
        return max(0,min(len(geometry)-1,lo))
    points=[]; last=-1e9
    for idx in severe:
        sm=samples[idx]; mid=(sm["start"]+sm["end"])/2
        if mid-last < max(180,total_d/14): continue
        c=geometry[geom_index(mid)]
        if len(c)>=2:
            points.append([float(c[0]),float(c[1])]); last=mid
        if len(points)>=6: break
    # Compact ribbon for the client.
    segments=[]
    for sm in samples[:120]:
        if sm["score"] < 40: continue
        i0=geom_index(sm["start"]); i1=max(i0+1,geom_index(sm["end"])); i1=min(len(geometry)-1,i1)
        coords=geometry[i0:i1+1]
        if len(coords)<2: continue
        segments.append({"score":round(sm["score"],1),"level":traffic_level(sm["score"]),"coordinates":coords})
        if len(segments)>=40: break
    return {
        "traffic_score":round(score,1),"traffic_level":traffic_level(score),"congested_distance_km":round(congested/1000,2),
        "traffic_points":points,"traffic_segments":segments,"severe_segments":len(severe),"traffic_source":"mapbox-driving-traffic",
    }


def live_flow_metrics(route, flow_samples):
    coords = ((route or {}).get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2:
        return {"live_flow_score":0,"live_flow_cells":0,"live_flow_confidence":0}
    prepared = prepare_route_geometry(route)
    hits=[]
    for raw in flow_samples or []:
        row=_dict(raw)
        try:
            lat,lon=float(row["cell_lat"]),float(row["cell_lon"]); speed=float(row.get("avg_speed") or 0); sources=int(row.get("sources") or 0)
        except Exception:
            continue
        if not prepared.near_bbox(lat, lon, 190): continue
        d=prepared.distance_m(lat,lon)
        if d>190: continue
        score=92 if speed<8 else 72 if speed<18 else 48 if speed<32 else 18
        weight=min(8.0,sources)*max(.25,1-d/220)
        hits.append((score,weight,sources))
    if not hits:
        return {"live_flow_score":0,"live_flow_cells":0,"live_flow_confidence":0}
    total_w=sum(x[1] for x in hits)
    score=sum(x[0]*x[1] for x in hits)/max(total_w,1)
    distinct=sum(min(5,x[2]) for x in hits)
    confidence=clamp(18+distinct*3.5+len(hits)*4,0,100)
    return {"live_flow_score":round(score,1),"live_flow_cells":len(hits),"live_flow_confidence":round(confidence,1)}


def compact_steps(route):
    out=[]
    for leg in route.get("legs") or []:
        for step in leg.get("steps") or []:
            m=step.get("maneuver") or {}
            out.append({
                "distance":float(step.get("distance") or 0),"duration":float(step.get("duration") or 0),"name":str(step.get("name") or "")[:160],
                "maneuver":{"instruction":str(m.get("instruction") or "Continue pela rota")[:280],"type":m.get("type"),"modifier":m.get("modifier"),"location":m.get("location"),"bearing_after":m.get("bearing_after")},
            })
            if len(out)>=160: return out
    return out


def _blocked_zone_points(zones, local_hour):
    points=[]
    for raw in zones or []:
        z=_dict(raw)
        if not z.get("block_routes") or not _zone_active(z,local_hour): continue
        try: points.append([float(z["longitude"]),float(z["latitude"])])
        except Exception: pass
    return points[:18]


def _route_block_hits(route,zones,local_hour):
    coords=((route or {}).get("geometry") or {}).get("coordinates") or []
    prepared=prepare_route_geometry(route)
    hits=[]
    for raw in zones or []:
        z=_dict(raw)
        if not z.get("block_routes") or not _zone_active(z,local_hour): continue
        try:
            lat=float(z["latitude"]); lon=float(z["longitude"]); radius=clamp(float(z.get("radius_m") or 350),80,5000)
            if not prepared.near_bbox(lat,lon,radius): continue
            d=prepared.distance_m(lat,lon)
        except Exception: continue
        if d<=radius:
            hits.append({"id":z.get("id"),"name":z.get("name"),"distance_to_route_m":round(d)})
    return hits


def _apply_admin_blocks(routes,zones,local_hour):
    evaluated=[]
    for route in routes or []:
        route["_admin_block_hits"]=_route_block_hits(route,zones,local_hour)
        evaluated.append(route)
    clear=[r for r in evaluated if not r["_admin_block_hits"]]
    if clear:
        return clear,{"enforced":True,"fallback":False,"filtered":len(evaluated)-len(clear)}
    return evaluated,{"enforced":bool(evaluated),"fallback":bool(evaluated),"filtered":0}


def _verified_safety_points(reports,zones,local_hour,max_points=10):
    scored=[]
    now=datetime.now(timezone.utc)
    for raw in reports or []:
        r=_dict(raw); category=str(r.get("category") or "other")
        if category in {"blitz","traffic","crowd"}: continue
        try:
            sev=int(r.get("severity") or 0); conf=int(r.get("confirmations") or 0); lat=float(r["latitude"]); lon=float(r["longitude"])
        except Exception: continue
        age_h=max(0,(now-(_parse_iso(r.get("created_at")) or now)).total_seconds()/3600)
        freshness=math.exp(-age_h/(24*7))
        if sev>=4 and (conf>=2 or category in {"flood","road_block","construction","accident"}) and freshness>=.12:
            scored.append((sev*20+conf*3+freshness*15,[lon,lat]))
    for raw in zones or []:
        z=_dict(raw)
        if not _zone_active(z,local_hour): continue
        try:
            conf=float(z.get("confidence") or 0); danger=int(z.get("danger_level") or 2); lon=float(z["longitude"]); lat=float(z["latitude"])
        except Exception: continue
        if conf>=.65 and danger>=3:
            scored.append((danger*22+conf*20,[lon,lat]))
    scored.sort(key=lambda x:-x[0])
    out=[]
    for _,p in scored:
        if all(haversine_m(p[1],p[0],q[1],q[0])>100 for q in out): out.append(p)
        if len(out)>=max_points: break
    return out


class RouteEngine:
    def __init__(self, provider, max_display_routes=6, micro_route_budget=4,
                 micro_min_eta_gain_s=20, adaptive_variant_budget=2,
                 safety_variant_budget=2, candidate_limit=14, variant_workers=6):
        self.provider = provider
        self.max_display_routes = max(1, min(8, int(max_display_routes)))
        self.micro_route_budget = max(0, min(8, int(micro_route_budget)))
        self.micro_min_eta_gain_s = max(0, min(180, int(micro_min_eta_gain_s)))
        self.adaptive_variant_budget = max(0, min(4, int(adaptive_variant_budget)))
        self.safety_variant_budget = max(0, min(5, int(safety_variant_budget)))
        self.candidate_limit = max(6, min(20, int(candidate_limit)))
        # One shared executor bounds variant threads across every simultaneous
        # route job. V3 created nested executors per request, which could explode
        # to dozens of threads on a small Render worker under load.
        self._variant_pool = ThreadPoolExecutor(
            max_workers=max(2, min(16, int(variant_workers))),
            thread_name_prefix="vaigo-variant",
        )

    @staticmethod
    def _user_excludes(job, include_professional_policy=False):
        profile = job["profile"]
        prefs = job["preferences"]
        extra = []
        if prefs.get("avoid_ferries"):
            extra.append("ferry")
        if prefs.get("avoid_tolls") and profile in {"driving", "motorcycle"}:
            extra.append("toll")
        if profile == "motorcycle" or (prefs.get("avoid_unpaved") and profile in {"driving", "motorcycle"}):
            extra.append("unpaved")
        if include_professional_policy and job.get("professional_driver") and profile in {"driving", "motorcycle"}:
            extra.append("unpaved")
        return list(dict.fromkeys(extra))

    def _base_routes(self, job):
        """Fetch the provider's unbiased ETA candidate set.

        Admin/security exclusion points are intentionally *not* applied here.
        That keeps `fastest` faithful to the Central contract: fastest is ETA
        first, while safest/smart apply safety policy at selection time and via
        dedicated safety variants.
        """
        start, end = job["start"], job["end"]
        extra = self._user_excludes(job, include_professional_policy=False)
        routes = self.provider.routes(
            start["lon"], start["lat"], end["lon"], end["lat"],
            job["profile"], job.get("depart_at", "now"), True,
            None, extra, job.get("heading"), job.get("speed"), job.get("reroute", False),
        )
        return routes, extra

    @staticmethod
    def _remember_traffic(route, metrics):
        if isinstance(route, dict):
            route["_traffic_metrics_v4"] = metrics
        return metrics

    def _traffic(self, route):
        cached = (route or {}).get("_traffic_metrics_v4") if isinstance(route, dict) else None
        if isinstance(cached, dict):
            return cached
        return self._remember_traffic(route, route_traffic_metrics(route))

    def _variant_specs(self, base, job, context, modes, extra):
        if not base or job["profile"] not in {"driving", "motorcycle"}:
            return [], 1.0, 0.0
        baseline = min(base, key=lambda r: float(r.get("duration") or 10**12))
        baseline_s = max(1.0, float(baseline.get("duration") or 1))
        traffic = self._traffic(baseline)
        traffic_score = float(traffic.get("traffic_score") or 0)
        severe = int(traffic.get("severe_segments") or 0)
        specs = []

        # Micro-route probes: only spend provider calls when congestion can
        # plausibly produce a real time saving.
        points = list(traffic.get("traffic_points") or [])
        if self.micro_route_budget > 0 and baseline_s >= 300 and points and (traffic_score >= 34 or severe > 0):
            if traffic_score >= 82 or severe >= 5:
                dyn = 4
            elif traffic_score >= 68 or severe >= 3:
                dyn = 4
            elif traffic_score >= 52 or severe >= 2:
                dyn = 3
            else:
                dyn = 2
            budget = min(self.micro_route_budget, dyn)
            micro = []
            for pt in points[:min(3, budget)]:
                micro.append(([pt], "block"))
            if len(points) >= 2 and len(micro) < budget:
                micro.append((points[:2], "adjacent-blocks"))
            if len(points) >= 3 and len(micro) < budget:
                micro.append((points[:3], "short-corridor"))
            min_gain = max(self.micro_min_eta_gain_s, 30 if baseline_s >= 1200 else 20 if baseline_s >= 600 else 12)
            for pts, kind in micro[:budget]:
                specs.append({"kind": "micro", "points": pts, "extra": extra, "min_gain": min_gain, "strategy": kind})

        # Generic adaptive probes are useful only when Mapbox gives too few
        # distinct corridors and the trip has meaningful congestion/duration.
        if job.get("adaptive", True) and self.adaptive_variant_budget > 0:
            distinct = select_diverse_routes(base, max_routes=6, max_overlap=.91, sort_key=lambda r: float(r.get("duration") or 10**12))
            if len(distinct) < 3 and baseline_s >= 420 and (traffic_score >= 42 or severe > 0):
                coords = ((baseline.get("geometry") or {}).get("coordinates") or [])
                if len(coords) >= 12:
                    fractions = (.38, .66, .78, .25)[:self.adaptive_variant_budget]
                    for frac in fractions:
                        i = max(2, min(len(coords) - 3, int((len(coords) - 1) * frac)))
                        c = coords[i]
                        specs.append({"kind": "adaptive", "points": [[float(c[0]), float(c[1])]], "extra": extra})

        need_safety = any(m != "fastest" for m in modes)
        if need_safety:
            block_points = _blocked_zone_points(context.get("risk_zones"), job.get("local_hour"))
            verified = _verified_safety_points(
                context.get("reports"), context.get("risk_zones"), job.get("local_hour"),
                max_points=max(4, self.safety_variant_budget + 2),
            )
            # Deduplicate block + verified points while keeping mandatory admin
            # blocks first. These variants are not used to constrain fastest.
            safety_points = []
            for p in list(block_points) + list(verified):
                try:
                    q = [float(p[0]), float(p[1])]
                except Exception:
                    continue
                if all(haversine_m(q[1], q[0], x[1], x[0]) > 80 for x in safety_points):
                    safety_points.append(q)
            safe_extra = self._user_excludes(job, include_professional_policy=True)
            if self.safety_variant_budget > 0 and safety_points:
                groups = [[p] for p in safety_points[:self.safety_variant_budget]]
                if len(safety_points) >= 2 and len(groups) < self.safety_variant_budget:
                    groups.append(safety_points[:2])
                fixed_blocks = list(block_points)[:12]
                for group in groups[:self.safety_variant_budget]:
                    combined = []
                    for p in fixed_blocks + group:
                        if p not in combined:
                            combined.append(p)
                    specs.append({"kind": "safety", "points": combined[:18], "extra": safe_extra, "group_size": len(group)})
            elif job.get("professional_driver") and "unpaved" in safe_extra and "unpaved" not in extra:
                # Preserve the Central's professional-driver policy even when
                # there are no explicit safety points in this corridor.
                specs.append({"kind": "policy", "points": [], "extra": safe_extra})

        return specs, baseline_s, traffic_score

    def _fetch_variant(self, spec, job, baseline_s, baseline_traffic):
        try:
            found = self.provider.routes(
                job["start"]["lon"], job["start"]["lat"],
                job["end"]["lon"], job["end"]["lat"],
                job["profile"], job.get("depart_at", "now"), False,
                spec.get("points") or None, spec.get("extra") or None,
                job.get("heading"), job.get("speed"), job.get("reroute", False),
            )
            if not found:
                return None
            route = found[0]
            duration = float(route.get("duration") or 10**12)
            kind = spec["kind"]
            if kind == "micro":
                eta_gain = baseline_s - duration
                if eta_gain < float(spec.get("min_gain") or 0):
                    return None
                after = self._traffic(route)
                relief = baseline_traffic - float(after.get("traffic_score") or 0)
                route["_micro_route"] = True
                route["_micro_strategy"] = spec.get("strategy") or "block"
                route["_micro_avoided_points"] = len(spec.get("points") or [])
                route["_micro_traffic_relief"] = round(relief, 1)
                route["_eta_gain_s"] = round(eta_gain, 1)
            elif kind == "adaptive":
                if duration > baseline_s * 1.35:
                    return None
                route["_adaptive_variant"] = True
                route["_adaptive_avoided_points"] = len(spec.get("points") or [])
            elif kind == "safety":
                if duration > baseline_s * 1.52:
                    return None
                route["_safety_variant"] = True
                route["_safety_avoided_points"] = int(spec.get("group_size") or len(spec.get("points") or []))
            elif kind == "policy":
                if duration > baseline_s * 1.52:
                    return None
                route["_safety_variant"] = True
                route["_safety_avoided_points"] = 0
            return route
        except Exception:
            return None

    def _variants(self, base, job, context, modes, extra):
        specs, baseline_s, baseline_traffic = self._variant_specs(base, job, context, modes, extra)
        if not specs:
            return []
        futures = [self._variant_pool.submit(self._fetch_variant, spec, job, baseline_s, baseline_traffic) for spec in specs]
        fetched = []
        for future in futures:
            try:
                route = future.result()
            except Exception:
                route = None
            if route:
                fetched.append(route)
        # Strategy-level dedupe after the shared provider wave.
        out = []
        seen = {route_signature(r) for r in base if route_signature(r)}
        for route in sorted(fetched, key=lambda r: float(r.get("duration") or 10**12)):
            sig = route_signature(route)
            if sig and sig in seen:
                continue
            if route.get("_adaptive_variant") and any(route_overlap_ratio(route, x) >= .94 for x in base + out):
                continue
            if sig:
                seen.add(sig)
            out.append(route)
        return out

    def _enrich(self, routes, job, context, calculate_safety=True):
        enriched = []
        motorized = job["profile"] in {"driving", "motorcycle"}
        for idx, route in enumerate(routes[:self.candidate_limit]):
            if motorized:
                traffic = dict(self._traffic(route))
                flow = live_flow_metrics(route, context.get("flow_samples"))
            else:
                traffic = {"traffic_score": 0, "traffic_level": "—", "congested_distance_km": 0, "severe_segments": 0, "traffic_segments": []}
                flow = {"live_flow_score": 0, "live_flow_cells": 0, "live_flow_confidence": 0}
            if flow.get("live_flow_cells"):
                mapbox_score = float(traffic.get("traffic_score") or 0)
                live = float(flow.get("live_flow_score") or 0)
                conf = float(flow.get("live_flow_confidence") or 0) / 100
                blend = min(.34, .10 + conf * .24)
                traffic["traffic_score_provider"] = round(mapbox_score, 1)
                traffic["traffic_score"] = round(clamp(mapbox_score * (1 - blend) + live * blend if mapbox_score > 0 else live, 0, 100), 1)
                traffic["traffic_level"] = traffic_level(traffic["traffic_score"])
            if calculate_safety:
                safety = route_risk_metrics(route, context.get("reports"), context.get("risk_zones"), job.get("local_hour"), job["profile"])
            else:
                safety = {
                    "safety_score": None, "safety_conservative_score": None, "safety_level": None,
                    "quiet_score": None, "data_confidence": None, "nearby_alerts": [],
                    "risk_zones": [], "risk_factors": [], "safety_engine": "skipped-fastest",
                }
            enriched.append({
                "id": idx,
                "distance": float(route.get("distance") or 0),
                "duration": float(route.get("duration") or 0),
                "duration_min": round(float(route.get("duration") or 0) / 60, 1),
                "geometry": route.get("geometry"),
                "steps": compact_steps(route),
                "profile": job["profile"],
                "routing_provider": route.get("_provider", "mapbox"),
                "routing_profile_used": route.get("_profile_used", ""),
                "route_signature": route_signature(route),
                "micro_route": bool(route.get("_micro_route")),
                "micro_strategy": route.get("_micro_strategy") or "",
                "micro_avoided_points": int(route.get("_micro_avoided_points") or 0),
                "micro_traffic_relief": round(float(route.get("_micro_traffic_relief") or 0), 1),
                "eta_gain_s": round(float(route.get("_eta_gain_s") or 0), 1),
                "eta_gain_min": round(float(route.get("_eta_gain_s") or 0) / 60, 1),
                "adaptive_variant": bool(route.get("_adaptive_variant")),
                "adaptive_avoided_points": int(route.get("_adaptive_avoided_points") or 0),
                "safety_variant": bool(route.get("_safety_variant")),
                "safety_avoided_points": int(route.get("_safety_avoided_points") or 0),
                "admin_block_hits": route.get("_admin_block_hits") or [],
                **{k: v for k, v in traffic.items() if k != "traffic_points"},
                **flow,
                **safety,
            })
        return enriched

    @staticmethod
    def _apply_smart_scores(routes, safety_bias=68, traffic_bias=62):
        if not routes:
            return
        fastest = min(max(float(r.get("duration") or 0), 1) for r in routes)
        shortest = min(max(float(r.get("distance") or 0), 1) for r in routes)
        safety_bias = clamp(float(safety_bias), 0, 100)
        traffic_bias = clamp(float(traffic_bias), 0, 100)
        ws = .34 + safety_bias / 100 * .25
        wt = .38 - safety_bias / 100 * .16
        wtr = .04 + traffic_bias / 100 * .11
        wd, wc = .08, .07
        total = ws + wt + wtr + wd + wc
        for r in routes:
            dur = max(float(r.get("duration") or 0), 1)
            dist = max(float(r.get("distance") or 0), 1)
            safe = clamp(float(r.get("safety_conservative_score") or 58), 0, 100)
            eta = clamp(100 * fastest / dur, 45, 100)
            det = clamp(100 - max(0, dist / shortest - 1) * 150, 45, 100)
            traf = 100 - clamp(float(r.get("traffic_score") or 0), 0, 100)
            conf = clamp(float(r.get("data_confidence") or 40), 0, 100)
            r["spark_score"] = round(clamp((safe * ws + eta * wt + traf * wtr + det * wd + conf * wc) / total, 0, 100), 1)

    @staticmethod
    def _mode_pool(enriched, mode):
        if mode == "fastest":
            return enriched, {"enforced": False, "fallback": False, "filtered": 0, "bypassed_for_eta": True}
        clear = [r for r in enriched if not (r.get("admin_block_hits") or [])]
        if clear:
            return clear, {"enforced": True, "fallback": False, "filtered": len(enriched) - len(clear)}
        return enriched, {"enforced": bool(enriched), "fallback": bool(enriched), "filtered": 0}

    def _select_mode(self, enriched, mode, job):
        pool, admin_policy = self._mode_pool(enriched, mode)
        fastest = min(enriched, key=lambda r: r["duration"])
        fastest_s = max(1, fastest["duration"])
        if mode == "fastest":
            ordered = sorted(pool, key=lambda r: r["duration"])
            selected = ordered[0]
        elif mode == "safest":
            cap = 1.62 if job.get("night_active") else 1.52
            eligible = [r for r in pool if r["duration"] <= fastest_s * cap] or pool
            ordered = sorted(eligible, key=lambda r: (-float(r.get("safety_conservative_score") or 0), r["duration"]))
            selected = ordered[0]
        elif mode == "quietest":
            eligible = [r for r in pool if r["duration"] <= fastest_s * 1.45] or pool
            ordered = sorted(eligible, key=lambda r: (-float(r.get("quiet_score") or 0), r["duration"]))
            selected = ordered[0]
        else:
            ordered = sorted(pool, key=lambda r: (-float(r.get("spark_score") or 0), r["duration"]))
            selected = ordered[0]
        return fastest, ordered, selected, admin_policy

    @staticmethod
    def _public_route(raw):
        # Shallow-copy large immutable payload pieces (geometry/steps) instead of
        # deepcopying them for every mode in a precalc bundle. Internal caches
        # always use underscore-prefixed keys and are never exposed.
        return {k: v for k, v in raw.items() if not str(k).startswith("_")}

    def _format_result(self, enriched, mode, job, started, direction_guard, context, bundle_shared=False):
        fastest, ordered, selected, admin_policy = self._select_mode(enriched, mode, job)
        display = []
        for raw in ordered:
            if not display or not any(route_overlap_ratio(raw, x) >= .985 for x in display):
                display.append(raw)
            if len(display) >= self.max_display_routes:
                break
        selected_sig = selected.get("route_signature")
        if selected_sig and not any(r.get("route_signature") == selected_sig for r in display):
            display.insert(0, selected)
        public = []
        selected_id = 0
        for i, raw in enumerate(display):
            r = self._public_route(raw)
            r["id"] = i
            r["badges"] = []
            if r.get("route_signature") == selected_sig:
                r["badges"].append(mode)
                selected_id = i
            if r.get("route_signature") == fastest.get("route_signature"):
                r["badges"].append("fastest")
            if r.get("micro_route"):
                r["badges"].append("micro")
            r["eta_gain_s"] = round(max(0.0, float(r.get("eta_gain_s") or 0)), 1)
            r["eta_gain_min"] = round(r["eta_gain_s"] / 60, 1)
            if mode == "fastest":
                # Match the Central's local fast engine contract. Even when a
                # precalc bundle also computed safety for other modes, the ETA
                # result stays explicitly safety-neutral and cache-safe.
                for key in (
                    "risk", "observed_safety_score", "safety_score",
                    "safety_conservative_score", "safety_level",
                    "safety_level_label", "data_confidence",
                    "data_confidence_label", "uncertainty_penalty",
                    "risk_exposure_pct", "hotspot_risk", "cluster_risk",
                    "evidence_count", "evidence_density", "quiet_score",
                    "nearby_alerts", "risk_zones", "risk_factors",
                    "safety_engine", "spark_score",
                ):
                    r.pop(key, None)
                r.update({
                    "fast_eta_only": True,
                    "safety_score": None,
                    "safety_conservative_score": None,
                    "safety_level": None,
                    "safety_level_label": "Não analisado no modo Rápida",
                    "data_confidence": 0,
                    "risk_exposure_pct": 0,
                    "hotspot_risk": 0,
                    "risk_zones": [],
                    "risk_factors": [],
                    "nearby_alerts": [],
                    "quiet_score": 0,
                    "spark_score": 0,
                })
            public.append(r)
        return {
            "ok": True,
            "request_id": job.get("request_id"),
            "routes": public,
            "selected_id": selected_id,
            "mode": mode,
            "profile": job["profile"],
            "provider": selected.get("routing_provider", "mapbox"),
            "engine": "vaigo-route-node-v4",
            "processing_ms": round((time.perf_counter() - started) * 1000, 1),
            "direction_guard": direction_guard,
            "admin_area_policy": admin_policy,
            "bundle_shared": bool(bundle_shared),
            "context": {
                "source": context.get("source", "none"),
                "reports": len(context.get("reports") or []),
                "risk_zones": len(context.get("risk_zones") or []),
                "live_flow_cells": len(context.get("flow_samples") or []),
            },
        }

    def calculate_bundle(self, job, context=None, modes=None):
        started = time.perf_counter()
        context = context or {"reports": [], "risk_zones": [], "flow_samples": [], "source": "none"}
        modes = [m for m in (modes or [job.get("mode", "safest")]) if m in {"fastest", "safest", "smart", "quietest"}]
        modes = list(dict.fromkeys(modes)) or ["safest"]
        need_safety = any(m != "fastest" for m in modes)

        base, extra = self._base_routes(job)
        # If the provider's base request was already slow, return a strong base
        # result instead of spending a second network wave and missing the
        # Central's route deadline. Normal fast Mapbox responses still get the
        # full micro/adaptive/safety exploration.
        base_elapsed = time.perf_counter() - started
        variants = self._variants(base, job, context, modes, extra) if base_elapsed < 3.2 else []
        candidates = list(base) + variants

        exact = {}
        for route in candidates:
            sig = route_signature(route) or f"anon-{id(route)}"
            old = exact.get(sig)
            if old is None or float(route.get("duration") or 10**12) < float(old.get("duration") or 10**12):
                exact[sig] = route
        routes = sorted(exact.values(), key=lambda r: float(r.get("duration") or 10**12))[:self.candidate_limit]

        # Mark admin-zone intersections, but defer filtering until mode selection
        # so fastest remains pure ETA while safe/smart obey the policy.
        for route in routes:
            route["_admin_block_hits"] = _route_block_hits(route, context.get("risk_zones"), job.get("local_hour"))
        if job["profile"] in {"driving", "motorcycle"}:
            routes, direction_guard = apply_start_direction_guard(routes, job.get("heading"), job.get("speed"))
        else:
            direction_guard = {"active": False, "rejected": 0}
        if not routes:
            raise RuntimeError("Nenhuma rota válida após as regras do node.")

        enriched = self._enrich(routes, job, context, calculate_safety=need_safety)
        if need_safety:
            self._apply_smart_scores(enriched, job.get("safety_bias", 68), job.get("traffic_bias", 62))
        return {
            mode: self._format_result(
                enriched, mode, job, started, direction_guard, context,
                bundle_shared=len(modes) > 1,
            )
            for mode in modes
        }

    def calculate(self, job, context=None):
        mode = job.get("mode", "safest")
        return self.calculate_bundle(job, context, [mode])[mode]


def normalize_job(payload):
    payload = dict(payload or {})

    def coord(name):
        raw = payload.get(name) or {}
        lat = float(raw["lat"])
        lon = float(raw["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"{name} inválido")
        return {"lat": lat, "lon": lon}

    start, end = coord("start"), coord("end")
    profile = str(payload.get("profile") or "driving").strip().lower()
    if profile not in {"walking", "cycling", "driving", "motorcycle"}:
        profile = "driving"
    mode = str(payload.get("mode") or "safest").strip().lower()
    if mode not in {"fastest", "safest", "smart", "quietest"}:
        mode = "safest"
    prefs = dict(payload.get("preferences") or {})
    try:
        local_hour = int(payload.get("local_hour")) if payload.get("local_hour") is not None else None
    except Exception:
        local_hour = None
    if local_hour is not None and not 0 <= local_hour <= 23:
        local_hour = None
    try:
        safety_bias = clamp(float(payload.get("safety_bias", 68)), 0, 100)
        traffic_bias = clamp(float(payload.get("traffic_bias", 62)), 0, 100)
    except Exception:
        safety_bias, traffic_bias = 68, 62
    request_id = str(payload.get("request_id") or hashlib.sha256(json.dumps([start, end, mode, profile, time.time_ns()]).encode()).hexdigest()[:18])[:80]
    return {
        "request_id": request_id,
        "start": start,
        "end": end,
        "profile": profile,
        "mode": mode,
        "depart_at": str(payload.get("depart_at") or "now")[:32],
        "heading": sanitize_bearing(payload.get("heading")),
        "speed": sanitize_speed(payload.get("speed")),
        "reroute": bool(payload.get("reroute")),
        "adaptive": payload.get("adaptive", True) is not False,
        "preferences": {
            "avoid_ferries": bool(prefs.get("avoid_ferries")),
            "avoid_tolls": bool(prefs.get("avoid_tolls")),
            "avoid_unpaved": bool(prefs.get("avoid_unpaved")),
        },
        "professional_driver": bool(payload.get("professional_driver")),
        "local_hour": local_hour,
        "night_active": bool(payload.get("night_active")),
        "safety_bias": safety_bias,
        "traffic_bias": traffic_bias,
    }
