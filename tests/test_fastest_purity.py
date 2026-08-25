import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_engine import RouteEngine, normalize_job


def mk_route(coords, duration, distance=1200, congestion=20):
    n = max(1, len(coords)-1)
    return {
        "distance": distance,
        "duration": duration,
        "geometry": {"type":"LineString","coordinates":coords},
        "legs": [{"steps": [], "annotation": {
            "distance": [distance/n]*n,
            "duration": [duration/n]*n,
            "speed": [10]*n,
            "congestion_numeric": [congestion]*n,
            "congestion": ["low"]*n,
        }}],
        "_profile_used":"driving-traffic",
        "_provider":"mapbox",
    }


class Provider:
    def __init__(self):
        self.straight = mk_route([[0,0],[.005,0],[.01,0]],600,1100)
        self.detour = mk_route([[0,0],[.003,.005],[.007,.005],[.01,0]],690,1500)
    def routes(self, *args, **kwargs):
        alternatives = args[6] if len(args) > 6 else kwargs.get("alternatives", True)
        exclusions = args[7] if len(args) > 7 else kwargs.get("exclusions")
        if alternatives:
            return copy.deepcopy([self.straight, self.detour])
        if exclusions:
            return copy.deepcopy([self.detour])
        return copy.deepcopy([self.detour])


def main():
    engine = RouteEngine(Provider(), micro_route_budget=0, adaptive_variant_budget=0, safety_variant_budget=2)
    job = normalize_job({
        "request_id":"purity",
        "start":{"lat":0,"lon":0},
        "end":{"lat":0,"lon":.01},
        "profile":"driving",
        "mode":"safest",
        "adaptive":False,
        "local_hour":15,
    })
    context = {
        "reports":[],
        "risk_zones":[{
            "id": 7, "name":"Bloqueio admin", "latitude":0, "longitude":.005,
            "radius_m":180, "level_cap":1, "confidence":.95, "danger_level":5,
            "block_routes":1, "active":1, "start_hour":None, "end_hour":None,
        }],
        "flow_samples":[], "source":"test",
    }
    out = engine.calculate_bundle(job, context, ["fastest","safest"])
    fast = out["fastest"]["routes"][out["fastest"]["selected_id"]]
    safe = out["safest"]["routes"][out["safest"]["selected_id"]]
    assert fast["duration"] == 600, fast
    assert fast.get("fast_eta_only") is True
    assert fast.get("safety_level") is None
    assert fast.get("nearby_alerts") == []
    assert safe["duration"] == 690, safe
    assert out["fastest"]["admin_area_policy"].get("bypassed_for_eta") is True
    assert out["safest"]["admin_area_policy"].get("enforced") is True
    print("test_fastest_purity: OK")


if __name__ == "__main__":
    main()
