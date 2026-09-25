"""Cross-check: does the dependency-free optimiser find the same minimum as scipy?

Not part of the test suite (it needs scipy). Usage: python3 dev/xcheck_scipy.py DATA_DIR/PERSON
"""
import os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import effort_calibration as ec
from scipy.optimize import minimize

workouts, days, info = ec.read_person(sys.argv[1])
data, _, _ = ec.prepare("x", workouts, days, info)
mine = ec.fit(data)
best, rng = None, random.Random(1)
for _ in range(12):
    x0 = [v + rng.uniform(-1.5, 1.5) for v in ec.pack(ec.START)]
    for method in ("Powell", "L-BFGS-B"):
        r = minimize(lambda t: ec.objective(t, data), x0, method=method)
        if best is None or r.fun < best.fun:
            best = r
theirs = ec.unpack(best.x)
print(f"objective: stdlib {ec.objective(ec.pack(mine), data):.6f}, scipy {best.fun:.6f}")
for name in ec.PARAM_NAMES:
    print(f"{name:5s} stdlib {mine[name]:.4f}  scipy {theirs[name]:.4f}")
for name, minutes in ec.SCENARIOS:
    print(f"{name:22s} stdlib {ec.scenario(minutes, mine)[1]:.2f}  scipy {ec.scenario(minutes, theirs)[1]:.2f}")
