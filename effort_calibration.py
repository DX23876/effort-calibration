"""Fit a recalibrated Effort curve to shared WHOOP exports (ryanbr/noop#2438).

Input: one folder per contributor, each holding the two files from the share page / script
(whoop_workouts_shared.csv, whoop_days_shared.csv) and optionally info.txt, the comment text the
page produces. Output: a Markdown report comparing the fitted curve with today's formula.

    python3 effort_calibration.py DATA_DIR [--out report.md]

Model (step 1 of the plan: curve and zone weights fitted together):

    load  = minutes x (share below zone 1 x w0 + sum over zones of share x exp(beta x (x - 55) / 10))
    b     = a x ln(1 + load / k)
    score = 21 x b / (b^p + 21^p)^(1/p)          (approaches 21, never reaches it)

WHOOP changed how it defines its zones: older exports use % of max heart rate, newer ones % of
heart-rate reserve. The export carries no dates, so each contributor's switch is found from the data:
the average heart rate of a workout is compared with what its zone shares imply under either
definition, and the single change point that fits best is used. Older workouts are then moved to
their real intensity before fitting.

Fitted: a, k, beta and w0 (0 <= w0 <= 1, so time below zone 1 never counts more than zone 1). The
soft-cap exponent p is a fixed design choice, P_CAP = 6: it keeps 21 out of reach with room between
19 and 21, and the few very hard workouts in any export cannot pin it down. Every WHOOP band (light,
moderate, high, all-out) carries the same total weight in the loss, so the rare hard workouts are
not drowned out by the many light ones.

x is the zone's intensity in % of heart-rate reserve (55, 65, ... 95 for WHOOP's default zones), so
custom zones can be placed at their real intensity. Today's formula is Edwards (weights 1..5, zero
below 50 % HRR) through 21 x ln(1 + TRIMP) / ln(7201).

Pure standard library, so it runs wherever python3 does and in tools-python.yml.
"""
import csv, math, os, random, re, sys, unicodedata

TODAY_DENOMINATOR = 7201.0
DEFAULT_MIDPOINTS = [55.0, 65.0, 75.0, 85.0, 95.0]  # % HRR, WHOOP's default zones 50-60 ... 90-100
BANDS = [(18.0, "all-out"), (14.0, "high"), (10.0, "moderate"), (0.0, "light")]

# WHOOP adds muscular load to Strength Trainer sessions, so their strain is not heart-rate only.
STRENGTH_WORDS = ["strength", "weight", "lifting", "gewicht", "kraft", "muscul", "pesas", "levantamento", "halter"]


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
    """WHOOP 'auto' max HR stand-in per day: the second-highest daily max over the trailing window.

    WHOOP's zones follow the max heart rate actually reached lately, and move down again when nobody
    goes to their limit for a while. On a five-year export, zones implied by this trailing value
    matched each workout's average heart rate far better (2.5 / 5.8 bpm for the older / newer zone
    definition) than one value for all years (11.7 / 14.9 bpm). The second-highest keeps a single
    artefact day from setting it.
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
    skipped = {"strength": 0, "incomplete": 0, "custom zones without bounds": 0, "no resting HR where needed": 0}
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
        out.append({
            "person": person, "day": day, "month": w.get("month") or "", "strain": strain, "minutes": minutes,
            "old_zones": old, "generic": is_generic(w.get("activity")),
            "below": max(0.0, 1.0 - sum(z)), "shares": z, "mids": mids,
            "edwards": minutes * sum(s * edwards_weight(x) for s, x in zip(z, mids)),
        })
    skipped["generic name, kept"] = generic_kept
    return out, skipped, (switch_day, switch_gain)


# ---------------------------------------------------------------- model

PARAM_NAMES = ["a", "k", "beta", "w0"]
P_CAP = 6.0

def unpack(theta, p=None):
    w0 = 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, theta[3]))))
    return {"a": math.exp(theta[0]), "k": math.exp(theta[1]), "beta": theta[2], "w0": w0, "p": p or P_CAP}

def pack(params):
    w0 = min(max(params["w0"], 1e-9), 1 - 1e-9)
    return [math.log(params["a"]), math.log(params["k"]), params["beta"], math.log(w0 / (1 - w0))]

def load(w, prm, cache=None):
    key = tuple(w["mids"])
    zw = cache.get(key) if cache is not None else None
    if zw is None:
        zw = [math.exp(prm["beta"] * (x - 55.0) / 10.0) for x in key]
        if cache is not None:
            cache[key] = zw
    return w["minutes"] * (w["below"] * prm["w0"] + sum(s * z for s, z in zip(w["shares"], zw)))

def curve(load_value, prm):
    b = prm["a"] * math.log1p(max(0.0, load_value) / prm["k"])
    if b <= 0:
        return 0.0
    p, m = prm["p"], max(b, 21.0)  # scaled by the larger term so large p cannot overflow
    # Below 21 mathematically; the clamp only stops float rounding from printing 21.000000000000004.
    return min(21.0 * b / (m * ((b / m) ** p + (21.0 / m) ** p) ** (1.0 / p)), math.nextafter(21.0, 0.0))

def today_score(trimp):
    return 21.0 * math.log1p(max(0.0, trimp)) / math.log(TODAY_DENOMINATOR)

def predict(w, prm, cache=None):
    return curve(load(w, prm, cache), prm)

def huber(r, delta=1.0):
    a = abs(r)
    return 0.5 * r * r if a <= delta else delta * (a - 0.5 * delta)

def objective(theta, data, balanced=True, p=None):
    """Huber loss. balanced: each WHOOP band carries the same total weight (the default)."""
    try:
        prm, cache = unpack(theta, p), {}
        if not balanced:
            return sum(huber(w["strain"] - predict(w, prm, cache)) for w in data) / len(data)
        per_band = {}
        for w in data:
            per_band.setdefault(w["band"], []).append(huber(w["strain"] - predict(w, prm, cache)))
        return sum(sum(v) / len(v) for v in per_band.values()) / len(per_band)
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

START = {"a": 4.5, "k": 15.0, "beta": 0.7, "w0": 0.5}

def fit(data, starts=4, seed=0, balanced=True, p=None):
    """Fit a, k, beta and w0 with a robust (Huber) loss; several starts guard against local minima."""
    for w in data:
        w.setdefault("band", band(w["strain"]))
    f = lambda t: objective(t, data, balanced, p)
    rng = random.Random(seed)
    best = None
    for s in range(starts):
        x0 = pack(START)
        if s:
            x0 = [v + rng.uniform(-0.7, 0.7) for v in x0]
        theta, val = nelder_mead(f, x0)
        theta, val = nelder_mead(f, theta, step=0.1)  # restart to polish
        if best is None or val < best[1]:
            best = (theta, val)
    return unpack(best[0], p)


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
        e["small_training_set"] = len(train) < MIN_TRAIN_WORKOUTS or sum(w["strain"] >= 14 for w in train) < MIN_TRAIN_HARD
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

def scenario(minutes, prm):
    total = sum(minutes)
    w = {"minutes": total, "below": minutes[0] / total, "shares": [m / total for m in minutes[1:]], "mids": DEFAULT_MIDPOINTS}
    edwards = sum(m * (i + 1) for i, m in enumerate(minutes[1:]))
    return today_score(edwards), predict(w, prm)


# ---------------------------------------------------------------- report

def report(data_dir):
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
    lines += [f"Contributors: {len(usable)}. Workouts used for the fit: {len(pooled)}.", ""]
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
    lines += ["", "### Fitted parameters", "", "| " + " | ".join(PARAM_NAMES) + " | p (fixed) |", "|" + "---|" * (len(PARAM_NAMES) + 1)]
    lines.append("| " + " | ".join(f"{prm[n]:.3g}" for n in PARAM_NAMES) + f" | {P_CAP:g} |")
    zw = [math.exp(prm["beta"] * (x - 55) / 10) for x in DEFAULT_MIDPOINTS]
    lines += ["", "Zone weights (below zone 1, zones 1-5): " + ", ".join(f"{v:.2f}" for v in [prm["w0"]] + zw), ""]
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
                  "| left out | " + " | ".join(PARAM_NAMES) + " |", "|---|" + "---|" * len(PARAM_NAMES)]
        for p, e in results.items():
            lines.append(f"| {p} | " + " | ".join(f"{e['params'][n]:.3g}" for n in PARAM_NAMES) + " |")
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
    lines += sensitivity(usable, pooled, prm, len(usable) >= 2, unverified)
    lines += ["", "### Example days (0-21)", "", "| day | today | fitted |", "|---|---|---|"]
    for name, minutes in SCENARIOS:
        t, n = scenario(minutes, prm)
        lines.append(f"| {name} | {t:.1f} | {n:.1f} |")
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
    lines = ["", "### Sensitivity", ""]
    if can_loo:
        lines += ["Leave-one-out summary for each variant (errors weighted by workouts):", "",
                  "| variant | error today | error new | right band today | right band new | bias on 14+ today | bias on 14+ new | contributors better than today |",
                  "|---|---|---|---|---|---|---|---|"]
        for label, args in (("band-balanced, p = 6 (default)", {}), ("unweighted, p = 6", {"balanced": False}),
                            ("band-balanced, p = 4", {"p": 4.0}), ("band-balanced, p = 8", {"p": 8.0})):
            m = _summary(leave_one_out(usable, unverified, starts=2, **args))
            lines.append(f"| {label} | {m['rmse_today']:.2f} | {m['rmse_new']:.2f} | {m['bands_today']:.0%} | {m['bands_new']:.0%} | "
                         f"{fmt(m['top_bias_today'])} | {fmt(m['top_bias_new'])} | {m['passed']} of {m['of']} |")
        lines.append("")
    lines += ["Example days under each variant (fit on all contributors):", "",
              "| day | unweighted | p = 4 | p = 6 | p = 8 |", "|---|---|---|---|---|"]
    variants = [fit(pooled, starts=2, balanced=False), fit(pooled, starts=2, p=4.0), prm, fit(pooled, starts=2, p=8.0)]
    for name, minutes in SCENARIOS:
        lines.append(f"| {name} | " + " | ".join(f"{scenario(minutes, v)[1]:.1f}" for v in variants) + " |")
    hard = [w for w in pooled if w["strain"] >= 14]
    rest = [w for w in pooled if w["strain"] < 14]
    if len(hard) >= 5:
        rng, spread = random.Random(seed), {n: [] for n in HARD_SCENARIOS}
        for r in range(reps):
            sample = rest + [rng.choice(hard) for _ in hard]
            v = fit(sample, starts=1, seed=r)
            for name, minutes in SCENARIOS:
                if name in spread:
                    spread[name].append(scenario(minutes, v)[1])
        lines += ["", f"Spread at the top: the {len(hard)} workouts at 14+ resampled {reps} times, everything else fixed "
                  "(range of the middle 90 %):", "", "| day | fitted | range |", "|---|---|---|"]
        for name, minutes in SCENARIOS:
            if name in spread:
                v = sorted(spread[name]); lo, hi = v[max(0, reps // 20)], v[min(reps - 1, reps - 1 - reps // 20)]
                lines.append(f"| {name} | {scenario(minutes, prm)[1]:.1f} | {lo:.1f} to {hi:.1f} |")
    return lines

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    text = report(sys.argv[1])
    if "--out" in sys.argv:
        open(sys.argv[sys.argv.index("--out") + 1], "w", encoding="utf-8").write(text)
    print(text)
