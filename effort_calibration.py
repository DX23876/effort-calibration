"""Fit a recalibrated Effort curve to shared WHOOP exports (ryanbr/noop#2438).

Input: one folder per contributor, each holding the two files from the share page / script
(whoop_workouts_shared.csv, whoop_days_shared.csv) and optionally info.txt, the comment text the
page produces. Output: a Markdown report comparing the fitted curve with today's formula.

    python3 effort_calibration.py DATA_DIR [--out report.md] [--variants]

--variants adds the leave-one-out table of every model variant compared in the issue (slow: one
leave-one-out run per variant).

Proposed model (PROPOSED below):

    weight(x) = exp(beta x (x - 55) / 10) for a minute at intensity x (% of heart-rate reserve), the
                same for zones 1-5 (at their midpoints 55 ... 95) and for the time below zone 1 (at
                its own intensity, estimated from the workout's average heart rate), switched off
                below 20 % HRR with a smooth step
    load      = minutes x weighted share
    b         = a x ln(1 + load / k)
    score     = 21 x b / (b^p + 21^p)^(1/p)       (p = 8: approaches 21, never reaches it)

Fitted: a, k and beta. The floor (20 % HRR) and the cap exponent are fixed design choices, compared
with their alternatives in the variant table. WHOOP scores workouts that stay close to resting heart
rate as 0; the floor reproduces that without a separate weight for the time below zone 1.

WHOOP changed how it defines its zones: older exports use % of max heart rate, newer ones % of
heart-rate reserve, and not on the same date for every account. The export carries no dates, so each
contributor's switch is found from the data: the average heart rate of a workout is compared with
what its zone shares imply under either definition, and the single change point that fits best is
used. Only workouts in the current definition train the proposed fit, because converting older zones
needs an estimated max heart rate. Older workouts are converted and scored, so they still check it.

Every WHOOP band (light, moderate, high, all-out) carries the same total weight in the loss, so the
rare hard workouts are not drowned out by the many light ones. Today's formula is Edwards (weights
1..5, zero below 50 % HRR) through 21 x ln(1 + TRIMP) / ln(7201).

Pure standard library, so it runs wherever python3 does and in tools-python.yml.
"""
import csv, math, os, random, re, sys, unicodedata

TODAY_DENOMINATOR = 7201.0
DEFAULT_MIDPOINTS = [55.0, 65.0, 75.0, 85.0, 95.0]  # % HRR, WHOOP's default zones 50-60 ... 90-100
BANDS = [(18.0, "all-out"), (14.0, "high"), (10.0, "moderate"), (0.0, "light")]

# WHOOP adds muscular load to Strength Trainer sessions, so their strain is not heart-rate only.
STRENGTH_WORDS = ["strength", "weight", "lifting", "gewicht", "kraft", "muscul", "pesas", "levantamento", "halter",
                  "bodybuilding", "culturismo"]


# ---------------------------------------------------------------- reading

def _norm(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()

def _num(s):
    try:
        v = float(str(s).strip())
        return v if math.isfinite(v) else None
    except ValueError:
        return None

def parse_info(text):
    """Read the comment text produced by the share page/script into a dict."""
    info = {"maxhr_mode": "unknown", "maxhr": None, "zones": "unknown", "bounds": None, "zones_since": "",
            "imported": "unknown", "sports": ""}
    for line in text.splitlines():
        line = line.strip().lstrip("-").strip()
        low = line.lower()
        if low.startswith("max hr setting"):
            val = low.split(":", 1)[1].strip()
            m = re.search(r"(\d{2,3})", val)
            info["maxhr_mode"] = "manual" if val.startswith("manual") else "auto" if val.startswith("auto") else "unknown"
            info["maxhr"] = float(m.group(1)) if (m and info["maxhr_mode"] == "manual") else None
        elif low.startswith("custom hr zones"):
            val = low.split(":", 1)[1].strip()
            info["zones"] = "unknown" if val.startswith("not") else "yes" if val.startswith("yes") else "no" if val.startswith("no") else "unknown"
            b = re.findall(r"z([1-5])\s+(\d{2,3})", val)
            if info["zones"] == "yes" and len(b) == 5:
                info["bounds"] = [float(v) for _, v in sorted(b)]
            since = re.search(r"since\s+(\d{4})-(\d{1,2})", val)
            if since:
                info["zones_since"] = f"{since.group(1)}-{int(since.group(2)):02d}"
        elif low.startswith("workouts imported"):
            val = low.split(":", 1)[1].strip()
            info["imported"] = "unknown" if val.startswith("not") else "yes" if val.startswith("yes") else "no" if val.startswith("no") else "unknown"
        elif low.startswith("main sports"):
            info["sports"] = line.split(":", 1)[1].strip()
    return info

def read_person(folder):
    with open(os.path.join(folder, "whoop_workouts_shared.csv"), newline="", encoding="utf-8") as f:
        workouts = list(csv.DictReader(f))
    with open(os.path.join(folder, "whoop_days_shared.csv"), newline="", encoding="utf-8") as f:
        days = list(csv.DictReader(f))
    info_path = os.path.join(folder, "info.txt")
    info = parse_info("")
    if os.path.exists(info_path):
        with open(info_path, encoding="utf-8") as f:
            info = parse_info(f.read())
    return workouts, days, info


# ---------------------------------------------------------------- preparing workouts

# WHOOP's catch-all names. They say nothing about the sport, so a strength session can hide behind them.
GENERIC_NAMES = {"activity", "other", "aktivitat", "sonstiges", "actividad", "otro", "otra", "activite", "autre",
                 "atividade", "outro", "outra", ""}

def is_generic(activity):
    return _norm(activity or "").strip() in GENERIC_NAMES

def has_no_names(workouts):
    """An export whose activity column is empty throughout: strength sessions cannot be told apart in it."""
    return bool(workouts) and all(not (w.get("activity") or "").strip() for w in workouts)

def is_strength(activity):
    a = _norm(activity or "")
    return any(w in a for w in STRENGTH_WORDS)

HRMAX_WINDOW_DAYS = 90

def trailing_hrmax(days, window=HRMAX_WINDOW_DAYS):
    """Max HR stand-in per day for 'auto' wearers: the second-highest daily max over the trailing window.

    Used only to place converted older zones and the time below zone 1. It is a stand-in, not WHOOP's
    value: WHOOP starts from Gellish's age formula and raises it from workout peaks with unpublished
    checks, and the exports cannot pin that value down. Inferring max HR from zone shares and average
    heart rate lands 7 to 17 bpm low on the exports whose manual max HR is known, so it cannot decide
    between rules either. The second-highest keeps a single artefact day from setting it.
    """
    peaks = sorted((int(_num(d["day"])), _num(d.get("max_hr"))) for d in days
                   if _num(d.get("day")) is not None and _num(d.get("max_hr")))
    cache = {}
    def at(day):
        if day is None:
            return None
        day = int(day)
        if day not in cache:
            recent = sorted((m for dd, m in peaks if day - window < dd <= day), reverse=True)
            cache[day] = recent[1] if len(recent) > 1 else (recent[0] if recent else None)
        return cache[day]
    return at

def zone_midpoints(info, rhr, hrmax):
    """Intensity (% HRR) of each of the contributor's five zones, or None when it can't be known."""
    if info["zones"] == "no" or info["zones"] == "unknown":
        return DEFAULT_MIDPOINTS
    if not info["bounds"] or not rhr or not hrmax or hrmax <= rhr:
        return None
    edges = info["bounds"] + [max(hrmax, info["bounds"][-1] + 1)]
    mids = [(edges[i] + edges[i + 1]) / 2 for i in range(5)]
    return [max(0.0, (m - rhr) / (hrmax - rhr) * 100.0) for m in mids]

MAXHR_MIDPOINTS = [0.55, 0.65, 0.75, 0.85, 0.95]  # WHOOP's older zones: 50-60 ... 90-100 % of max HR

def _implied_avg(shares, mids_bpm):
    inside = sum(shares)
    return sum(s * m for s, m in zip(shares, mids_bpm)) / inside if inside else None

def find_zone_switch(rows):
    """Index (in day order) where WHOOP's zones switch from % of max HR to % of HR reserve.

    rows: (day, shares 0-1, avg_hr, rhr, hrmax). Uses only workouts that sit almost entirely inside zones
    1-5, where the zone shares pin down the average heart rate. Returns (switch_day, gain) where gain
    is how much the split reduces the average-HR error against treating everything as HRR zones;
    switch_day None means no switch was found.
    """
    usable = []
    for day, shares, avg, rhr, hrmax in sorted(rows, key=lambda r: r[0]):
        if avg and rhr and hrmax and hrmax > rhr and sum(shares) >= 0.9:
            e_max = abs(_implied_avg(shares, [m * hrmax for m in MAXHR_MIDPOINTS]) - avg)
            e_hrr = abs(_implied_avg(shares, [rhr + m * (hrmax - rhr) for m in MAXHR_MIDPOINTS]) - avg)
            usable.append((day, e_max, e_hrr))
    if len(usable) < 20:
        return None, 0.0
    all_hrr = sum(u[2] for u in usable)
    best_k, best = 0, all_hrr
    running = all_hrr
    for k in range(1, len(usable) + 1):
        running += usable[k - 1][1] - usable[k - 1][2]
        if running < best:
            best_k, best = k, running
    gain = 1.0 - best / all_hrr if all_hrr else 0.0
    if best_k == 0 or gain < 0.15:
        return None, gain
    switch_day = usable[best_k][0] if best_k < len(usable) else float("inf")
    return switch_day, gain

def edwards_weight(x):
    """Today's Edwards weight for an intensity in % HRR."""
    return 0 if x < 50 else min(5, int((x - 50) // 10) + 1)

BELOW_MIN_SHARE = 0.15  # below this share of the workout, the time below zone 1 hardly matters and is not placed

def below_intensity(below, shares, mids, avg, rhr, hrmax):
    """Intensity (% HRR) of a workout's time below zone 1, from its average heart rate.

    The time inside zones 1-5 sits at the zone midpoints, so the rest of the average heart rate belongs to
    the time below zone 1. Clamped between resting heart rate and zone 1's lower edge (5 % below its
    midpoint). Returns None when it is needed but cannot be placed (no average or resting heart rate), and
    a fixed 10 % below zone 1's edge when the time below zone 1 is too short to place.
    """
    edge = mids[0] - 5.0
    if below < BELOW_MIN_SHARE:
        return edge - 10.0
    if not (avg and rhr and hrmax and hrmax > rhr):
        return None
    bpm = [rhr + m / 100.0 * (hrmax - rhr) for m in mids]
    below_bpm = (avg - sum(s * b for s, b in zip(shares, bpm))) / below
    return max(0.0, min(edge, (below_bpm - rhr) / (hrmax - rhr) * 100.0))

def prepare(person, workouts, days, info):
    """Turn a contributor's rows into fit-ready workouts, and count what was left out and why."""
    rhr_by_day = {d["day"]: _num(d.get("resting_hr")) for d in days}
    manual = info["maxhr"] if info["maxhr_mode"] == "manual" and info["maxhr"] else None
    trailing = trailing_hrmax(days)
    hrmax_on = lambda day: manual or trailing(day)
    # Custom zones apply from the month they were set; before that, WHOOP's own zones were in force.
    custom_on = lambda w: info["zones"] == "yes" and (not info["zones_since"] or (w.get("month") or "") >= info["zones_since"])
    switch_day, switch_gain = find_zone_switch(
        [(_num(w.get("day")), [(_num(w.get(f"z{i}")) or 0) / 100.0 for i in range(1, 6)],
          _num(w.get("avg_hr")), rhr_by_day.get(w.get("day")), hrmax_on(_num(w.get("day"))))
         for w in workouts if _num(w.get("day")) is not None and not custom_on(w)])
    # Generic names ("Activity", "Other") are kept: WHOOP's muscular load comes from Strength Trainer
    # sessions, which carry that name. Whether generic rows sit above the curve is checked in the report
    # rather than assumed.
    out = []
    skipped = {"strength": 0, "incomplete": 0, "custom zones without bounds": 0, "no resting HR where needed": 0,
               "no average HR": 0}
    generic_kept = 0
    for w in workouts:
        strain, minutes = _num(w.get("strain")), _num(w.get("duration_min"))
        shares = [_num(w.get(f"z{i}")) for i in range(1, 6)]
        if strain is None or not minutes or minutes <= 0 or any(s is None for s in shares) or sum(shares) > 105:
            skipped["incomplete"] += 1
            continue
        if is_strength(w.get("activity")):
            skipped["strength"] += 1
            continue
        if is_generic(w.get("activity")):
            generic_kept += 1
        rhr = rhr_by_day.get(w.get("day"))  # never substituted: a missing resting HR drops the row where it is needed
        day = _num(w.get("day"))
        hrmax = hrmax_on(day)
        custom = custom_on(w)
        if custom and not info["bounds"]:
            skipped["custom zones without bounds"] += 1
            continue
        old = not custom and switch_day is not None and day is not None and day < switch_day
        if (custom or old) and not (rhr and hrmax and hrmax > rhr):
            skipped["no resting HR where needed"] += 1
            continue
        if custom:
            mids = zone_midpoints(info, rhr, hrmax)
        elif old:
            mids = [max(0.0, (m * hrmax - rhr) / (hrmax - rhr) * 100.0) for m in MAXHR_MIDPOINTS]
        else:
            mids = DEFAULT_MIDPOINTS
        z = [s / 100.0 for s in shares]
        below = max(0.0, 1.0 - sum(z))
        xb = below_intensity(below, z, mids, _num(w.get("avg_hr")), rhr, hrmax)
        if xb is None:  # its time below zone 1 cannot be placed without a resting and an average heart rate
            skipped["no resting HR where needed" if not rhr else "no average HR"] += 1
            continue
        out.append({
            "person": person, "activity": w.get("activity") or "", "day": day, "month": w.get("month") or "",
            "strain": strain, "minutes": minutes, "old_zones": old, "generic": is_generic(w.get("activity")),
            "below": below, "xb": xb, "shares": z, "mids": mids,
            "edwards": minutes * sum(s * edwards_weight(x) for s, x in zip(z, mids)),
        })
    skipped["generic name, kept"] = generic_kept
    return out, skipped, (switch_day, switch_gain)


# ---------------------------------------------------------------- model

# A model is a spec (how a workout's minutes are weighted, which numbers are fitted, which workouts train it)
# plus the fitted numbers. Both live in one dict, prm, so every function below takes the same argument.
#
#   weights  zone weight for an intensity x in % HRR: "exp" e^(beta (x - 55) / 10), "edwards" 1 to 5 as
#            NOOP uses today, "banister" x e^(1.92 x) relative to x = 55 %
#   below    weight of the time below zone 1: "exp" the same exponential at that time's own intensity,
#            "w0" one fitted number, "banister" Banister at that time's intensity
#   floor    % HRR below which time counts nothing (smooth step), None for no floor
#   p        soft-cap exponent: score = 21 b / (b^p + 21^p)^(1/p)
#   fitted   the numbers the fit chooses; a and k always
#   train    "all" or "current zones": only workouts in WHOOP's current (% of reserve) zone definition train it
#   people   "equal": inside each band every contributor carries the same weight, not every workout

FLOOR_WIDTH = 3.0  # % HRR; width of the smooth step at the floor, so the loss stays continuous for the optimiser

def _spec(**kw):
    spec = {"weights": "exp", "below": "exp", "floor": 20.0, "p": 8.0, "fitted": ["a", "k", "beta"],
            "train": "current zones", "people": "workouts"}
    spec.update(kw)
    return spec

# The proposed model (ryanbr/noop#2438, 2026-10): one exponential for all intensities, nothing below 20 % HRR.
PROPOSED = _spec()
# The model published on 2026-09-26: one fitted weight (w0) for all time below zone 1, cap 6, all zones.
PUBLISHED = _spec(below="w0", floor=None, p=6.0, fitted=["a", "k", "beta", "w0"], train="all")

# Every variant compared in the issue, in the order of its table (python3 effort_calibration.py DIR --variants).
VARIANTS = [
    ("Edwards 1 to 5, only the curve refitted", _spec(weights="edwards", below="w0", floor=None, p=6.0, fitted=["a", "k", "w0"], train="all")),
    ("Banister weights", _spec(weights="banister", below="banister", floor=None, p=6.0, fitted=["a", "k"], train="all")),
    ("published model (w0, cap 6)", PUBLISHED),
    ("published model, current zones only", dict(PUBLISHED, train="current zones")),
    ("exponential below zone 1 without a floor, cap 6", _spec(floor=None, p=6.0, train="all")),
    ("floor at 20 %, current zones, cap 6", _spec(p=6.0)),
    ("floor at 20 %, current zones, cap 8 (proposed)", PROPOSED),
    ("floor at 20 %, current zones, cap 10", _spec(p=10.0)),
    ("floor at 30 %, current zones, cap 10", _spec(floor=30.0, p=10.0)),
    ("proposed, every person weighted equally", _spec(people="equal")),
]

START = {"a": 4.5, "k": 15.0, "beta": 0.7, "w0": 0.5}
SPEC_KEYS = ("weights", "below", "floor", "p", "fitted", "train", "people")

def spec_of(prm):
    return {k: prm[k] for k in SPEC_KEYS}

def unpack(theta, spec=PROPOSED):
    prm = dict(spec)
    for name, t in zip(spec["fitted"], theta):
        if name in ("a", "k"):
            prm[name] = math.exp(t)
        elif name == "w0":  # between 0 and 1: time below zone 1 never counts more than zone 1
            prm[name] = 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, t))))
        else:
            prm[name] = t
    return prm

def pack(params, spec=PROPOSED):
    out = []
    for name in spec["fitted"]:
        v = params[name]
        if name in ("a", "k"):
            out.append(math.log(v))
        elif name == "w0":
            v = min(max(v, 1e-9), 1 - 1e-9)
            out.append(math.log(v / (1 - v)))
        else:
            out.append(v)
    return out

def _sigmoid(z):
    return 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, z))))

def _banister(x):
    return (x / 100.0) * math.exp(1.92 * x / 100.0) / (0.55 * math.exp(1.92 * 0.55))

def zone_weight(x, prm):
    """Weight of one minute at intensity x (% HRR) inside zones 1 to 5."""
    if prm["weights"] == "edwards":
        return edwards_weight(x) or prm["w0"]  # converted older zones can fall below 50 % HRR
    if prm["weights"] == "banister":
        return _banister(x)
    return math.exp(prm["beta"] * (x - 55.0) / 10.0)

def below_weight(x, prm):
    """Weight of one minute below zone 1, at that time's estimated intensity x (% HRR)."""
    if prm["below"] == "w0":
        return prm["w0"]
    weight = _banister(x) if prm["below"] == "banister" else math.exp(prm["beta"] * (x - 55.0) / 10.0)
    return weight if prm["floor"] is None else weight * _sigmoid((x - prm["floor"]) / FLOOR_WIDTH)

def load(w, prm, cache=None):
    key = tuple(w["mids"])
    zw = cache.get(key) if cache is not None else None
    if zw is None:
        zw = [zone_weight(x, prm) for x in key]
        if cache is not None:
            cache[key] = zw
    return w["minutes"] * (w["below"] * below_weight(w.get("xb", 0.0), prm) + sum(s * z for s, z in zip(w["shares"], zw)))

def curve(load_value, prm):
    b = prm["a"] * math.log1p(max(0.0, load_value) / prm["k"])
    if b <= 0:
        return 0.0
    p, m = prm["p"], max(b, 21.0)  # scaled by the larger term so large p cannot overflow
    # Below 21 mathematically; the clamp only stops float rounding from printing 21.000000000000004.
    return min(21.0 * b / (m * ((b / m) ** p + (21.0 / m) ** p) ** (1.0 / p)), math.nextafter(21.0, 0.0))

def shown(score):
    """One decimal, but two near the top, rounded down there, so a score the curve keeps below 21 never
    prints as 21."""
    return f"{math.floor(score * 100) / 100:.2f}" if score >= 20.95 else f"{score:.1f}"

def today_score(trimp):
    return 21.0 * math.log1p(max(0.0, trimp)) / math.log(TODAY_DENOMINATOR)

def predict(w, prm, cache=None):
    return curve(load(w, prm, cache), prm)

def huber(r, delta=1.0):
    a = abs(r)
    return 0.5 * r * r if a <= delta else delta * (a - 0.5 * delta)

def objective(theta, data, spec=PROPOSED, balanced=True):
    """Huber loss. balanced: each WHOOP band carries the same total weight (the default). With
    spec["people"] == "equal", every contributor also carries the same weight inside a band."""
    try:
        prm, cache = unpack(theta, spec), {}
        if not balanced:
            return sum(huber(w["strain"] - predict(w, prm, cache)) for w in data) / len(data)
        per_band = {}
        for w in data:
            per_band.setdefault(w["band"], {}).setdefault(w["person"] if spec["people"] == "equal" else "", []).append(
                huber(w["strain"] - predict(w, prm, cache)))
        return sum(sum(sum(v) / len(v) for v in groups.values()) / len(groups) for groups in per_band.values()) / len(per_band)
    except (OverflowError, ValueError, ZeroDivisionError):
        return float("inf")

def nelder_mead(f, x0, step=0.5, iters=4000, tol=1e-10):
    n = len(x0)
    pts = [list(x0)] + [[x0[j] + (step if j == i else 0.0) for j in range(n)] for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol:
            break
        centroid = [sum(p[j] for p in pts[:-1]) / n for j in range(n)]
        refl = [centroid[j] + (centroid[j] - pts[-1][j]) for j in range(n)]
        fr = f(refl)
        if fr < vals[0]:
            exp_ = [centroid[j] + 2 * (centroid[j] - pts[-1][j]) for j in range(n)]
            fe = f(exp_)
            pts[-1], vals[-1] = (exp_, fe) if fe < fr else (refl, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = refl, fr
        else:
            con = [centroid[j] + 0.5 * (pts[-1][j] - centroid[j]) for j in range(n)]
            fc = f(con)
            if fc < vals[-1]:
                pts[-1], vals[-1] = con, fc
            else:
                pts = [pts[0]] + [[pts[0][j] + 0.5 * (p[j] - pts[0][j]) for j in range(n)] for p in pts[1:]]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    best = min(range(n + 1), key=lambda i: vals[i])
    return pts[best], vals[best]

def training_rows(data, spec):
    """The workouts a spec learns from: with train == "current zones", only WHOOP's % of reserve zones, so no
    fitted number depends on converting older zones through an estimated max heart rate."""
    return [w for w in data if spec["train"] == "all" or not w.get("old_zones")]

def fit(data, spec=PROPOSED, starts=4, seed=0, balanced=True):
    """Fit a spec's numbers with a robust (Huber) loss; several starts guard against local minima."""
    data = training_rows(data, spec)
    for w in data:
        w.setdefault("band", band(w["strain"]))
    f = lambda t: objective(t, data, spec, balanced)
    rng = random.Random(seed)
    best = None
    for s in range(starts):
        x0 = pack(START, spec)
        if s:
            x0 = [v + rng.uniform(-0.7, 0.7) for v in x0]
        theta, val = nelder_mead(f, x0)
        theta, val = nelder_mead(f, theta, step=0.1)  # restart to polish
        if best is None or val < best[1]:
            best = (theta, val)
    return unpack(best[0], spec)


# ---------------------------------------------------------------- evaluation

def band(score):
    return next(name for lo, name in BANDS if score >= lo)

def evaluate(data, prm):
    new = [predict(w, prm) for w in data]
    old = [today_score(w["edwards"]) for w in data]
    truth = [w["strain"] for w in data]
    rmse = lambda pred: math.sqrt(sum((p - t) ** 2 for p, t in zip(pred, truth)) / len(truth))
    bands = lambda pred: sum(band(p) == band(t) for p, t in zip(pred, truth)) / len(truth)
    top = [i for i, t in enumerate(truth) if t >= 14]
    top_bias = lambda pred: (sum(pred[i] - truth[i] for i in top) / len(top)) if top else None
    return {"n": len(data), "rmse_new": rmse(new), "rmse_today": rmse(old), "bands_new": bands(new),
            "bands_today": bands(old), "top_n": len(top), "top_bias_new": top_bias(new), "top_bias_today": top_bias(old)}

def passes(e):
    # ">=" on bands: a contributor already at 100 % cannot be beaten there, so a lower error must be enough.
    return e["rmse_new"] < e["rmse_today"] and e["bands_new"] >= e["bands_today"]

MIN_TRAIN_WORKOUTS, MIN_TRAIN_HARD = 100, 10

def leave_one_out(by_person, unverified=(), **fit_args):
    """Fit on everyone else, score the one left out. Needs at least two contributors.

    A row whose training set is small (under 100 workouts, or under 10 at 14+) is flagged, because a fit
    to a handful of light workouts says nothing about a held-out export with hard days.
    """
    results = {}
    for person in by_person:
        # Exports marked cardio-unverified are scored like everyone else but never train a fit.
        train = [w for p, ws in by_person.items() if p != person and p not in unverified for w in ws]
        prm = fit(train, **fit_args)
        e = evaluate(by_person[person], prm)
        e["params"] = prm
        used = training_rows(train, spec_of(prm))
        e["small_training_set"] = len(used) < MIN_TRAIN_WORKOUTS or sum(w["strain"] >= 14 for w in used) < MIN_TRAIN_HARD
        results[person] = e
    return results

def time_split(data):
    """One-person fallback: fit on the older half of the days, score the newer half."""
    ordered = sorted(data, key=lambda w: w["day"] if w["day"] is not None else -1)
    half = len(ordered) // 2
    return evaluate(ordered[half:], fit(ordered[:half]))

def drift_by_halfyear(data, prm):
    """Mean residual (WHOOP minus fit) per half-year, when the data carry months."""
    groups = {}
    for w in data:
        if w["month"]:
            key = w["month"][:4] + ("-H1" if w["month"][5:7] <= "06" else "-H2")
            groups.setdefault(key, []).append(w["strain"] - predict(w, prm))
    return {k: (sum(v) / len(v), len(v)) for k, v in sorted(groups.items())}

def drift(data, prm, parts=3):
    """Mean residual (WHOOP minus fit) per time third, oldest first. A trend means WHOOP changed."""
    ordered = sorted(data, key=lambda w: w["day"] if w["day"] is not None else -1)
    size = max(1, len(ordered) // parts)
    chunks = [ordered[i * size:(i + 1) * size if i < parts - 1 else None] for i in range(parts)]
    return [sum(w["strain"] - predict(w, prm) for w in c) / len(c) for c in chunks if c]

def day_gap(workouts, days, prepared, prm):
    """Median of WHOOP Day Strain minus the fitted curve on the day's summed workout load (step 2 context)."""
    by_day = {}
    for w in prepared:
        by_day.setdefault(w["day"], 0.0)
        by_day[w["day"]] += load(w, prm)
    strain = {_num(d["day"]): _num(d.get("day_strain")) for d in days}
    gaps = sorted(strain[d] - curve(l, prm) for d, l in by_day.items() if strain.get(d) is not None)
    return gaps[len(gaps) // 2] if gaps else None

# Minutes per zone: (below zone 1, zone 1 .. zone 5)
SCENARIOS = [
    ("30-minute brisk walk", (0, 30, 0, 0, 0, 0)),
    ("45-minute walk", (0, 20, 25, 0, 0, 0)),
    ("1 h zone 3", (0, 0, 0, 60, 0, 0)),
    ("1 h zone 4", (0, 0, 0, 0, 60, 0)),
    ("90 min intervals", (0, 0, 20, 30, 30, 10)),
    ("Marathon 3:30", (0, 0, 0, 30, 150, 30)),
    ("10 h ultra", (0, 0, 300, 300, 0, 0)),
    ("24 h at >= 90 % HRR", (0, 0, 0, 0, 0, 1440)),
]

# Time below zone 1 only, at a given intensity (% HRR): what the floor does
BELOW_SCENARIOS = [
    ("60 min stroll at 15 % HRR", (60, 0, 0, 0, 0, 0), 15.0),
    ("60 min easy walk at 35 % HRR", (60, 0, 0, 0, 0, 0), 35.0),
]

def scenario(minutes, prm, xb=0.0):
    """(today, new) for a day of minutes per zone; xb is the intensity (% HRR) of the time below zone 1."""
    total = sum(minutes)
    w = {"minutes": total, "below": minutes[0] / total, "xb": xb, "shares": [m / total for m in minutes[1:]],
         "mids": DEFAULT_MIDPOINTS}
    edwards = sum(m * (i + 1) for i, m in enumerate(minutes[1:]))
    return today_score(edwards), predict(w, prm)


# ---------------------------------------------------------------- report

def report(data_dir, variants=False):
    people, prepared, skipped, raw, switches, unverified = {}, {}, {}, {}, {}, set()
    for name in sorted(os.listdir(data_dir)):
        folder = os.path.join(data_dir, name)
        if not os.path.isdir(folder):
            continue
        workouts, days, info = read_person(folder)
        prepared[name], skipped[name], switches[name] = prepare(name, workouts, days, info)
        people[name], raw[name] = info, (workouts, days)
        if has_no_names(workouts):
            unverified.add(name)
    usable = {p: ws for p, ws in prepared.items() if ws}
    if not usable or not set(usable) - unverified:
        return "No usable workouts found."
    pooled = [w for p, ws in usable.items() if p not in unverified for w in ws]
    prm = fit(pooled)
    lines = ["## Effort calibration report", ""]
    lines += [f"Contributors: {len(usable)}. Workouts scored: {sum(len(ws) for ws in usable.values())}. "
              f"Workouts that train the fit: {len(training_rows(pooled, PROPOSED))} (WHOOP's current zones only).", ""]
    if unverified & set(usable):
        lines += ["Marked *cardio-unverified* (export has no activity names, so strength sessions cannot be left out): "
                  + ", ".join(sorted(unverified & set(usable))) + ". They are scored below but do not train any fit.", ""]
    lines += ["| contributor | workouts used | left out | generic names kept | max HR | custom zones | older WHOOP zones |",
              "|---|---|---|---|---|---|---|"]
    for p in usable:
        left = ", ".join(f"{v} {k}" for k, v in skipped[p].items() if v and k != "generic name, kept") or "none"
        generic = skipped[p].get("generic name, kept", 0)
        i = people[p]
        mx = f"manual {i['maxhr']:.0f}" if i["maxhr_mode"] == "manual" else i["maxhr_mode"]
        old = [w for w in usable[p] if w["old_zones"]]
        new = [w for w in usable[p] if not w["old_zones"]]
        until = max((w["month"] for w in old), default="") if old else ""
        since = min((w["month"] for w in new), default="") if new else ""
        span = f", until {until} / from {since}" if until and since else ""
        sw = f"{len(old)} workouts{span} (avg-HR fit {switches[p][1]:.0%} better)" if old else "none found"
        zones = i["zones"] + (f" since {i['zones_since']}" if i.get("zones_since") else "")
        lines.append(f"| {p} | {len(usable[p])} | {left} | {generic} | {mx} | {zones} | {sw} |")
    for p in prepared:
        if p not in usable:
            left = ", ".join(f"{v} {k}" for k, v in skipped[p].items() if v and k != "generic name, kept") or "no rows"
            lines.append(f"| {p} | 0 (not used) | {left} | - | - | - | - |")
    names = prm["fitted"]
    floor_text = "none" if prm["floor"] is None else f"{prm['floor']:g} % HRR"
    lines += ["", "### Fitted parameters", "", "| " + " | ".join(names) + " | floor (fixed) | p (fixed) |",
              "|" + "---|" * (len(names) + 2)]
    lines.append("| " + " | ".join(f"{prm[n]:.3g}" for n in names) + f" | {floor_text} | {prm['p']:g} |")
    lines += ["", "Weight of one minute, relative to zone 1's midpoint: below zone 1 at 15 / 30 / 45 % HRR "
              + " / ".join(f"{below_weight(x, prm):.2f}" for x in (15, 30, 45))
              + "; zones 1-5 " + ", ".join(f"{zone_weight(x, prm):.2f}" for x in DEFAULT_MIDPOINTS), ""]
    lines += ["### Validation", ""]
    if len(usable) >= 2:
        lines += ["Each contributor is scored by a fit made **without** their data (leave-one-out).", ""]
        results = leave_one_out(usable, unverified)
    else:
        lines += ["Only one contributor, so leave-one-out is not possible. As a stand-in, the fit is made on the "
                  "**older half** of the days and scored on the newer half. This checks the method, not the result.", ""]
        results = {next(iter(usable)) + " (newer half)": time_split(pooled)}
    lines += ["| contributor | workouts | error today | error new | right band today | right band new | workouts at 14+ | bias on 14+ today | bias on 14+ new | better than today |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    fmt = lambda v: "-" if v is None else f"{v:+.1f}"
    for p, e in results.items():
        verdict = ("yes" if passes(e) else "NO") + (" (small training set)" if e.get("small_training_set") else "")
        lines.append(f"| {p} | {e['n']} | {e['rmse_today']:.2f} | {e['rmse_new']:.2f} | {e['bands_today']:.0%} | "
                     f"{e['bands_new']:.0%} | {e['top_n']} | {fmt(e['top_bias_today'])} | {fmt(e['top_bias_new'])} | {verdict} |")
    if any(e.get("small_training_set") for e in results.values()):
        lines += ["", f"*Small training set: the fit that scored this row saw fewer than {MIN_TRAIN_WORKOUTS} workouts or fewer "
                  f"than {MIN_TRAIN_HARD} at 14+, so the row says little yet.*"]
    if len(usable) >= 2:
        lines += ["", "Parameters of each leave-one-out fit (how much one contributor moves the curve):", "",
                  "| left out | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
        for p, e in results.items():
            lines.append(f"| {p} | " + " | ".join(f"{e['params'][n]:.3g}" for n in names) + " |")
    lines += ["", "Error is the root-mean-square difference from WHOOP's Activity Strain, in points on the 0-21 axis. "
              "Bias on 14+ is the average difference on workouts WHOOP scored 14 or higher (negative = too low).", ""]
    lines += ["### Checks", ""]
    for p, ws in usable.items():
        d = drift(ws, prm)
        g = day_gap(*raw[p], ws, prm)
        lines.append(f"- {p}: mean residual by time third (oldest first) {', '.join(f'{v:+.2f}' for v in d)}; "
                     f"median Day Strain above the day's summed workouts {fmt(g)}.")
        hy = drift_by_halfyear(ws, prm)
        if hy:
            lines.append("  - by half-year: " + ", ".join(f"{k} {v:+.2f} (n={n})" for k, (v, n) in hy.items()))
    pooled_hy = drift_by_halfyear(pooled, prm)
    if pooled_hy:
        lines.append("- all contributors, by half-year: " + ", ".join(f"{k} {v:+.2f} (n={n})" for k, (v, n) in pooled_hy.items()))
    lines += generic_check(usable, prm)
    if len(usable) >= 2:
        lines += contributor_tables(usable, results)
    lines += sensitivity(usable, pooled, prm, len(usable) >= 2, unverified)
    if variants and len(usable) >= 2:
        lines += variant_table(usable, unverified)
    lines += ["", "### Example days (0-21)", "", "| day | today | fitted |", "|---|---|---|"]
    for name, minutes in SCENARIOS:
        t, n = scenario(minutes, prm)
        lines.append(f"| {name} | {shown(t)} | {shown(n)} |")
    for name, minutes, xb in BELOW_SCENARIOS:
        t, n = scenario(minutes, prm, xb)
        lines.append(f"| {name} | {shown(t)} | {shown(n)} |")
    return "\n".join(lines) + "\n"

HARD_SCENARIOS = ["1 h zone 4", "90 min intervals", "Marathon 3:30", "10 h ultra"]

def _summary(results):
    """Pooled leave-one-out numbers: mean error, band agreement and 14+ bias, weighted by workouts."""
    n = sum(e["n"] for e in results.values())
    top = sum(e["top_n"] for e in results.values())
    avg = lambda k: sum(e[k] * e["n"] for e in results.values()) / n
    bias = lambda k: (sum(e[k] * e["top_n"] for e in results.values() if e["top_n"]) / top) if top else None
    return {"rmse_today": avg("rmse_today"), "rmse_new": avg("rmse_new"), "bands_today": avg("bands_today"),
            "bands_new": avg("bands_new"), "top_bias_today": bias("top_bias_today"), "top_bias_new": bias("top_bias_new"),
            "passed": sum(passes(e) for e in results.values()), "of": len(results)}

def generic_check(usable, prm, min_n=10, limit=1.0):
    """Do workouts with a generic name sit above the curve, compared with named ones? That is what hidden
    strength sessions (muscular load on top of heart rate) would look like."""
    lines = ["", "### Generic activity names", "",
             "Mean residual (WHOOP minus fit) of workouts with a generic name against named ones, per contributor. "
             f"A contributor is flagged when the generic ones sit more than {limit:g} point above the named ones "
             f"(with at least {min_n} of each).", "",
             "| contributor | generic | named | difference | flag |", "|---|---|---|---|---|"]
    mean = lambda ws: sum(w["strain"] - predict(w, prm) for w in ws) / len(ws)
    for p, ws in usable.items():
        g = [w for w in ws if w["generic"]]
        n = [w for w in ws if not w["generic"]]
        if not g:
            continue
        gm, nm = mean(g), (mean(n) if n else None)
        diff = gm - nm if nm is not None else None
        flag = "above named" if diff is not None and diff > limit and len(g) >= min_n and len(n) >= min_n else ""
        lines.append(f"| {p} | {gm:+.2f} (n={len(g)}) | " + (f"{nm:+.2f} (n={len(n)})" if n else "-") + " | "
                     + (f"{diff:+.2f}" if diff is not None else "-") + f" | {flag} |")
    return lines

def sensitivity(usable, pooled, prm, can_loo, unverified=(), reps=20, seed=0):
    """The checks asked for in review: band weighting on/off, the fixed cap p, and the spread at the top."""
    fmt = lambda v: "-" if v is None else f"{v:+.1f}"
    checks = [("band-balanced, cap 8 (default)", PROPOSED, True), ("unweighted, cap 8", PROPOSED, False),
              ("band-balanced, cap 6", dict(PROPOSED, p=6.0), True), ("band-balanced, cap 10", dict(PROPOSED, p=10.0), True)]
    lines = ["", "### Sensitivity", ""]
    if can_loo:
        lines += ["Leave-one-out summary for each check (errors weighted by workouts):", "",
                  "| check | error today | error new | right band today | right band new | bias on 14+ today | bias on 14+ new | contributors better than today |",
                  "|---|---|---|---|---|---|---|---|"]
        for label, spec, balanced in checks:
            m = _summary(leave_one_out(usable, unverified, spec=spec, balanced=balanced, starts=2))
            lines.append(f"| {label} | {m['rmse_today']:.2f} | {m['rmse_new']:.2f} | {m['bands_today']:.0%} | {m['bands_new']:.0%} | "
                         f"{fmt(m['top_bias_today'])} | {fmt(m['top_bias_new'])} | {m['passed']} of {m['of']} |")
        lines.append("")
    fits = [prm if (spec is PROPOSED and balanced) else fit(pooled, spec=spec, balanced=balanced, starts=2)
            for _, spec, balanced in checks]
    lines += ["Example days under each check (fit on all contributors):", "",
              "| day | " + " | ".join(label for label, _, _ in checks) + " |", "|---|" + "---|" * len(checks)]
    for name, minutes in SCENARIOS:
        lines.append(f"| {name} | " + " | ".join(shown(scenario(minutes, v)[1]) for v in fits) + " |")
    hard = [w for w in training_rows(pooled, PROPOSED) if w["strain"] >= 14]
    rest = [w for w in training_rows(pooled, PROPOSED) if w["strain"] < 14]
    if len(hard) >= 5:
        rng, spread = random.Random(seed), {n: [] for n in HARD_SCENARIOS}
        for r in range(reps):
            sample = rest + [rng.choice(hard) for _ in hard]
            v = fit(sample, starts=1, seed=r)
            for name, minutes in SCENARIOS:
                if name in spread:
                    spread[name].append(scenario(minutes, v)[1])
        lines += ["", f"Spread at the top: the {len(hard)} training workouts at 14+ resampled {reps} times, everything else "
                  "fixed (range of the middle 90 %):", "", "| day | fitted | range |", "|---|---|---|"]
        for name, minutes in SCENARIOS:
            if name in spread:
                v = sorted(spread[name]); lo, hi = v[max(0, reps // 20)], v[min(reps - 1, reps - 1 - reps // 20)]
                lines.append(f"| {name} | {shown(scenario(minutes, prm)[1])} | {shown(lo)} to {shown(hi)} |")
    return lines

def contributor_tables(usable, results):
    """What the new curve means for each contributor: average WHOOP, today and new score of their own hard
    (14+) and light (below 10) workouts, each scored by the fit made without them."""
    rows = {p: [(w["strain"], today_score(w["edwards"]), predict(w, results[p]["params"])) for w in ws]
            for p, ws in usable.items() if p in results}
    lines = []
    for title, lo, hi in (("Hard workouts (WHOOP 14 or more)", 14.0, 99.0), ("Light workouts (WHOOP below 10)", 0.0, 10.0)):
        lines += ["", f"### {title}", "", "| contributor | workouts | WHOOP | NOOP today | NOOP new |", "|---|---|---|---|---|"]
        picked = {p: [r for r in rs if lo <= r[0] < hi] for p, rs in rows.items()}
        for p in sorted(picked, key=lambda q: (-len([r for r in rows[q] if r[0] >= 14]), -len(rows[q]))):
            rs = picked[p]
            if rs:
                n = len(rs)
                lines.append(f"| {p} | {n} | {sum(r[0] for r in rs) / n:.1f} | {sum(r[1] for r in rs) / n:.1f} | "
                             f"**{sum(r[2] for r in rs) / n:.1f}** |")
        everyone = [r for rs in picked.values() for r in rs]
        if everyone:
            closer = sum(abs(r[2] - r[0]) < abs(r[1] - r[0]) for r in everyone)
            in_band = lambda i: sum(band(r[i]) == band(r[0]) for r in everyone)
            off = lambda i: sum(r[i] - r[0] for r in everyone) / len(everyone)
            lines += ["", f"{closer} of {len(everyone)} closer to WHOOP than today; in WHOOP's band today {in_band(1)}, "
                      f"new {in_band(2)}; off by {off(1):+.2f} today, {off(2):+.2f} new."]
    return lines

def variant_table(usable, unverified=()):
    """Every variant compared in the issue, scored the same way: leave-one-out, every workout counted once."""
    everyone = [w for ws in usable.values() for w in ws]
    truth = [w["strain"] for w in everyone]
    def pooled(pred):
        n = len(truth)
        top = [p - t for p, t in zip(pred, truth) if t >= 14]
        return (math.sqrt(sum((p - t) ** 2 for p, t in zip(pred, truth)) / n),
                sum(band(p) == band(t) for p, t in zip(pred, truth)) / n, sum(top) / len(top) if top else None)
    fmt = lambda v: "-" if v is None else f"{v:+.1f}"
    rm, bd, top = pooled([today_score(w["edwards"]) for w in everyone])
    lines = ["", "### Every variant", "",
             "Leave-one-out over every contributor, every workout counted once (so it differs a little from the "
             "per-contributor averages above).", "",
             "| variant | error | right band | off by at 14+ | better than today |", "|---|---|---|---|---|",
             f"| NOOP today | {rm:.2f} | {bd:.0%} | {fmt(top)} | |"]
    for label, spec in VARIANTS:
        res = leave_one_out(usable, unverified, spec=spec)
        pred = [predict(w, res[p]["params"]) for p, ws in usable.items() for w in ws]
        rm, bd, top = pooled(pred)
        lines.append(f"| {label} | {rm:.2f} | {bd:.0%} | {fmt(top)} | {sum(passes(e) for e in res.values())} of {len(res)} |")
    return lines

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    text = report(sys.argv[1], variants="--variants" in sys.argv)
    if "--out" in sys.argv:
        open(sys.argv[sys.argv.index("--out") + 1], "w", encoding="utf-8").write(text)
    print(text)
