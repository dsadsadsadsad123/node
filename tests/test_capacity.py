import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import CapacityGate


def main():
    gate = CapacityGate(4)
    assert gate.try_acquire()
    assert gate.try_acquire()
    assert gate.try_acquire()
    assert gate.try_acquire()
    assert not gate.try_acquire()
    s = gate.snapshot()
    assert s["active_jobs"] == 4
    assert s["available_slots"] == 0
    gate.release(); gate.release(); gate.release(); gate.release()
    assert gate.snapshot()["active_jobs"] == 0
    print("test_capacity: OK")


if __name__ == "__main__":
    main()
