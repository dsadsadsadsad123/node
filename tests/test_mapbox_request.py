import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mapbox_provider import MapboxProvider


class FakeResponse:
    status_code = 200
    def raise_for_status(self):
        return None
    def json(self):
        return {
            "code": "Ok",
            "routes": [{
                "distance": 1000,
                "duration": 600,
                "geometry": {"type": "LineString", "coordinates": [[-46.7,-23.6],[-46.69,-23.6]]},
                "legs": [{"steps": [], "annotation": {"distance": [], "duration": [], "speed": []}}]
            }]
        }


class FakeSession:
    def __init__(self):
        self.calls = []
    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), timeout))
        return FakeResponse()


def main():
    p = MapboxProvider("pk.test", timeout_s=10)
    p.session = FakeSession()
    out = p.routes(
        -46.7,-23.6,-46.69,-23.6,
        profile="driving", depart_at="now", alternatives=True,
        exclusions=[[-46.695,-23.6]], extra_excludes=["toll"],
        start_bearing=90, start_speed=8, reroute=True,
    )
    assert out and out[0]["_provider"] == "mapbox"
    assert len(p.session.calls) == 1
    _, params, _ = p.session.calls[0]
    assert params["bearings"] == "90,70;"
    assert params["continue_straight"] == "true"
    assert 45 <= int(params["avoid_maneuver_radius"]) <= 160
    assert "toll" in params["exclude"]
    assert "point(" in params["exclude"]
    assert params["access_token"] == "pk.test"
    print("test_mapbox_request: OK")


if __name__ == "__main__":
    main()
