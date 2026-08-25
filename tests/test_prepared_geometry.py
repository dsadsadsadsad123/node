import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geo import PreparedGeometry, point_to_segment_distance_m


def exact(lat, lon, coords):
    return min(point_to_segment_distance_m(lat, lon, coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1]) for i in range(len(coords)-1))


def main():
    coords=[]
    for i in range(2400):
        x=i/2399*.08
        y=.0025*math.sin(i/55)
        coords.append([-46.7+x,-23.6+y])
    prepared=PreparedGeometry(coords,max_segments=900)
    probes=[(-23.6,-46.66),(-23.596,-46.64),(-23.603,-46.68),(-23.59,-46.62)]
    for lat,lon in probes:
        a=prepared.distance_m(lat,lon)
        b=exact(lat,lon,coords)
        assert abs(a-b) < 30, (a,b)
    print("test_prepared_geometry: OK")


if __name__ == "__main__":
    main()
