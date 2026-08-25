import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_engine import RouteEngine, normalize_job


def mk_route(coords, duration, distance=1200, congestion=35):
    return {
        "distance": distance,
        "duration": duration,
        "geometry": {"type": "LineString", "coordinates": coords},
        "legs": [{"steps": [], "annotation": {
            "distance": [distance/2, distance/2],
            "duration": [duration/2, duration/2],
            "speed": [10, 10],
            "congestion_numeric": [congestion, congestion],
            "congestion": ["moderate", "moderate"],
        }}],
        "_profile_used": "driving-traffic",
        "_provider": "mapbox",
    }


class CountingProvider:
    def __init__(self, routes):
        self.routes_data = routes
        self.calls = 0
    def routes(self, *args, **kwargs):
        self.calls += 1
        return copy.deepcopy(self.routes_data)


def main():
    provider = CountingProvider([
        mk_route([[0,0],[.01,0]], 600, 1200, 25),
        mk_route([[0,0],[.005,.003],[.01,0]], 660, 1400, 30),
        mk_route([[0,0],[.004,-.003],[.01,0]], 690, 1500, 32),
    ])
    engine = RouteEngine(provider, micro_route_budget=5, adaptive_variant_budget=2, safety_variant_budget=0)
    job = normalize_job({
        "request_id":"bundle-test","start":{"lat":0,"lon":0},"end":{"lat":0,"lon":.01},
        "profile":"driving","mode":"safest","adaptive":True
    })
    out = engine.calculate_bundle(job, {"reports":[],"risk_zones":[],"flow_samples":[],"source":"test"}, ["safest","fastest","smart"])
    assert set(out) == {"safest","fastest","smart"}
    # Base alternatives already have 3 diverse routes and light traffic, so V2
    # should not spend provider calls on adaptive/micro exploration.
    assert provider.calls == 1, provider.calls
    assert out["fastest"]["routes"][out["fastest"]["selected_id"]]["duration"] == 600
    assert all(v["bundle_shared"] for v in out.values())
    print("test_bundle_light: OK")


if __name__ == "__main__":
    main()
