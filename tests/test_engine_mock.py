import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import copy

from geo import apply_start_direction_guard
from route_engine import RouteEngine, normalize_job


class FakeProvider:
    def __init__(self, routes):
        self._routes = routes

    def routes(self, *args, **kwargs):
        return copy.deepcopy(self._routes)


def mk_route(coords, duration, distance=1200):
    return {
        "distance": distance,
        "duration": duration,
        "geometry": {"type": "LineString", "coordinates": coords},
        "legs": [{"steps": [], "annotation": {"distance": [], "duration": [], "speed": []}}],
        "_profile_used": "driving-traffic",
        "_provider": "mapbox",
    }


def test_fastest_selects_lowest_eta():
    a = mk_route([[0,0],[.01,0]], 600, 1000)
    b = mk_route([[0,0],[.005,.004],[.01,0]], 680, 1300)
    engine = RouteEngine(FakeProvider([a,b]), micro_route_budget=0)
    job = normalize_job({
        "request_id":"t-fast","start":{"lat":0,"lon":0},"end":{"lat":0,"lon":.01},
        "profile":"driving","mode":"fastest","adaptive":False
    })
    out = engine.calculate(job, {"reports":[],"risk_zones":[],"flow_samples":[],"source":"test"})
    assert out["routes"][out["selected_id"]]["duration"] == 600


def test_safest_can_choose_detour_away_from_verified_zone():
    straight = mk_route([[0,0],[.005,0],[.01,0]], 600, 1000)
    detour = mk_route([[0,0],[.003,.006],[.007,.006],[.01,0]], 690, 1400)
    engine = RouteEngine(FakeProvider([straight,detour]), micro_route_budget=0)
    job = normalize_job({
        "request_id":"t-safe","start":{"lat":0,"lon":0},"end":{"lat":0,"lon":.01},
        "profile":"driving","mode":"safest","adaptive":False,"local_hour":16
    })
    context = {
        "reports":[],
        "risk_zones":[{
            "id":1,"name":"Zona verificada","risk_type":"verified_incident_area","latitude":0,"longitude":.005,
            "radius_m":220,"level_cap":1,"confidence":.95,"source":"admin","start_hour":None,"end_hour":None,
            "danger_level":5,"block_routes":1,"active":1
        }],
        "flow_samples":[],"source":"test"
    }
    out = engine.calculate(job, context)
    selected = out["routes"][out["selected_id"]]
    assert selected["duration"] == 690, selected
    assert selected["safety_conservative_score"] > out["routes"][1]["safety_conservative_score"] if len(out["routes"]) > 1 else True


def test_direction_guard_rejects_opposite_start():
    east = mk_route([[0,0],[.002,0],[.01,0]], 600)
    west_first = mk_route([[0,0],[-.002,0],[.01,0]], 590)
    kept, meta = apply_start_direction_guard([west_first,east], bearing=90, speed=8)
    assert meta["active"] is True
    assert meta["rejected"] == 1
    assert len(kept) == 1
    assert kept[0]["duration"] == 600


if __name__ == "__main__":
    test_fastest_selects_lowest_eta()
    test_safest_can_choose_detour_away_from_verified_zone()
    test_direction_guard_rejects_opposite_start()
    print("test_engine_mock: OK")
