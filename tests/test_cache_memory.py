import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cache_backend import SharedCache


def main():
    cache=SharedCache("",default_ttl=10,memory_entries=64)
    src={"routes":[{"geometry":{"coordinates":[[1,2],[3,4]]},"badges":[]}]}
    cache.set("k",src)
    got=cache.get("k")
    got["routes"][0]["badges"].append("x")
    again=cache.get("k")
    assert again["routes"][0]["badges"] == []
    print("test_cache_memory: OK")


if __name__ == "__main__":
    main()
