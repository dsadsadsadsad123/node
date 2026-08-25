import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from route_engine import RouteEngine, normalize_job


def mk_route(coords, duration, distance=2400, congestion=85):
    n=max(2,len(coords)-1)
    return {
        "distance":distance,"duration":duration,
        "geometry":{"type":"LineString","coordinates":coords},
        "legs":[{"steps":[],"annotation":{
            "distance":[distance/n]*n,"duration":[duration/n]*n,"speed":[7]*n,
            "congestion_numeric":[congestion]*n,"congestion":["heavy"]*n,
        }}],"_profile_used":"driving-traffic","_provider":"mapbox",
    }


class MicroProvider:
    def __init__(self): self.calls=[]
    def routes(self,*args,**kwargs):
        # alternatives is positional arg #6 in RouteEngine calls
        alternatives = args[6] if len(args)>6 else kwargs.get('alternatives',True)
        self.calls.append(bool(alternatives))
        if alternatives:
            return [
                mk_route([[0,0],[.002,0],[.004,0],[.006,0],[.008,0],[.01,0]],900,2400,88),
                mk_route([[0,0],[.003,.002],[.006,.002],[.01,0]],960,2700,74),
                mk_route([[0,0],[.003,-.002],[.007,-.002],[.01,0]],990,2800,70),
            ]
        # A successful block bypass saves two minutes.
        return [mk_route([[0,0],[.003,.004],[.007,.004],[.01,0]],780,2300,38)]


def main():
    provider=MicroProvider()
    engine=RouteEngine(provider,micro_route_budget=4,micro_min_eta_gain_s=20,adaptive_variant_budget=0,safety_variant_budget=0)
    job=normalize_job({"request_id":"micro","start":{"lat":0,"lon":0},"end":{"lat":0,"lon":.01},"profile":"driving","mode":"fastest","adaptive":False})
    out=engine.calculate(job,{"reports":[],"risk_zones":[],"flow_samples":[],"source":"test"})
    selected=out['routes'][out['selected_id']]
    assert selected['duration']==780, selected
    assert selected['micro_route'] is True
    assert selected['eta_gain_s']>=120
    assert len(provider.calls)<=5, provider.calls
    print('test_micro_gain: OK')

if __name__=='__main__': main()
