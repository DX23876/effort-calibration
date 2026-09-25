# v4 - Reduces a WHOOP data export to the few columns needed to calibrate Effort.
# Keeps: activity name, duration, strain values, HR-zone shares and heart rates.
# Drops: dates, times, sleep, HRV, recovery, skin temp, SpO2, journal.
# Finds columns by header name (English, German, Spanish, French, Portuguese), so it works across
# export versions and app languages. Usage: python3 whoop_strain_share.py my_whoop_data.zip
import csv, io, re, sys, unicodedata, zipfile
from datetime import datetime

ALIASES = {
    "cycle_start": ["cycle_start_time", "startzeit_des_zyklus", "hora_de_inicio_del_ciclo", "heure_de_debut_du_cycle", "hora_de_inicio_do_ciclo"],
    "activity_name": ["activity_name", "name_der_aktivitat", "nombre_de_la_actividad", "nom_de_l_activite", "nome_da_atividade"],
    "activity_strain": ["activity_strain", "aktivitatsbelastung", "esfuerzo_de_la_actividad", "effort_activite", "esforco_da_atividade"],
    "workout_start": ["workout_start_time", "startzeit_des_trainings", "hora_de_inicio_del_entrenamiento", "heure_de_debut_de_l_entrainement", "hora_de_inicio_do_treino"],
    "workout_end": ["workout_end_time", "endzeit_des_trainings", "hora_de_finalizacion_del_entrenamiento", "heure_de_fin_de_l_entrainement", "hora_de_fim_do_treino"],
    "duration": ["duration_min", "dauer_min", "duracion_min", "duree_min", "duracao_min"],
    "max_hr": ["max_hr_bpm", "max_hf_schlage_pro_minute", "fc_max_lpm", "fc_max_bpm"],
    "avg_hr": ["average_hr_bpm", "durchschnittliche_hf_schlage_pro_minute", "fc_promedio_lpm", "fc_moyenne_bpm", "fc_media_bpm"],
    "day_strain": ["day_strain", "tagesbelastung", "esfuerzo_del_dia", "effort_du_jour", "esforco_diario"],
    "resting_hr": ["resting_heart_rate_bpm", "ruheherzfrequenz_schlage_pro_minute", "frecuencia_cardiaca_en_reposo_lpm", "frequence_cardiaque_au_repos_bpm", "frequencia_cardiaca_em_repouso_bpm"],
}

def norm(h):
    h = unicodedata.normalize("NFKD", h.replace("﻿", ""))
    h = "".join(c for c in h if not unicodedata.combining(c)).lower().replace("%", "pct")
    return re.sub(r"[^a-z0-9]+", "_", h).strip("_")

def columns(header):
    keys = [norm(h) for h in header]
    found = {f: keys.index(a) for f, names in ALIASES.items() for a in names if a in keys}
    for i, k in enumerate(keys):  # "HR Zone 1 %", "HF-Zone 1 %", "Zone FC 1 %", "Zona 1 de FC %" ...
        m = re.search(r"zon[ae]?_(?:[a-z]+_)*([1-5])_(?:[a-z]+_)*pct$", k)
        if m: found["z" + m.group(1)] = i
    found.setdefault("cycle_start", 0)  # every known layout starts with the cycle start
    return found

def read(name):
    text = z.read(name).decode("utf-8-sig")
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") > first.count(",") else ","
    return [r for r in csv.reader(io.StringIO(text), delimiter=delim) if r]

# Fallback for app languages the aliases don't cover: the current export has the same layout in every
# language, only translated, so a file with exactly this shape is read by position.
KNOWN_LAYOUTS = {
    "workouts": (17, 11, {"cycle_start": 0, "workout_start": 3, "workout_end": 4, "duration": 5, "activity_name": 6,
                          "activity_strain": 7, "max_hr": 9, "avg_hr": 10, "z1": 11, "z2": 12, "z3": 13, "z4": 14, "z5": 15}),
    "cycles": (26, 3, {"cycle_start": 0, "resting_hr": 4, "day_strain": 8, "max_hr": 10, "avg_hr": 11}),
}

def cell(r, c, key):
    i = c.get(key)
    return r[i] if i is not None and i < len(r) else ""

def stamp(s):
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?: (\d{1,2}):(\d{1,2})(?::(\d{1,2}))?|T(\d{1,2}):(\d{1,2}):(\d{1,2}))", s.strip()[:19])
    if not m: return None
    y, mo, d = (int(x) for x in m.group(1, 2, 3))
    h, mi, se = (int(x or 0) for x in (m.group(4, 5, 6) if m.group(4) else m.group(7, 8, 9)))
    try: return int((datetime(y, mo, d, h, mi, se) - datetime(1970, 1, 1)).total_seconds())
    except ValueError: return None

def minutes(r, c):
    t0, t1 = stamp(cell(r, c, "workout_start")), stamp(cell(r, c, "workout_end"))
    if t0 is None or t1 is None: return cell(r, c, "duration")
    if t1 < t0: return ""
    tenths = ((t1 - t0) * 10 + 30) // 60  # half-up to 0.1 min, integer maths so every implementation agrees
    return f"{tenths // 10}.{tenths % 10}"

z = zipfile.ZipFile(sys.argv[1])
workouts = cycles = None
for name in z.namelist():
    if not name.lower().endswith(".csv"): continue
    rows = read(name)
    if not rows: continue
    cols = columns(rows[0])
    if "activity_strain" in cols: workouts = (rows, cols)
    elif "day_strain" in cols: cycles = (rows, cols)
    else:
        for kind, (n, pct_col, pos) in KNOWN_LAYOUTS.items():
            if len(rows[0]) == n and "%" in rows[0][pct_col] and (workouts if kind == "workouts" else cycles) is None:
                if kind == "workouts": workouts = (rows, pos)
                else: cycles = (rows, pos)

need_w = ["activity_name", "activity_strain", "max_hr", "avg_hr", "z1", "z2", "z3", "z4", "z5"]
need_c = ["day_strain", "resting_hr", "max_hr", "avg_hr"]
for label, table, need in (("workouts", workouts, need_w), ("cycles", cycles, need_c)):
    missing = need if table is None else [f for f in need if f not in table[1]]
    if table is not None and not (("workout_start" in table[1] and "workout_end" in table[1]) or "duration" in table[1]) and label == "workouts":
        missing.append("duration")
    if missing:
        print(f"{label}: could not find {missing}. Please post the header lines of your export in the issue:")
        for name in z.namelist():
            if name.lower().endswith(".csv"):
                print(" ", name, "->", (read(name) or [[]])[0])
        sys.exit(1)

(wr, wc), (cr, cc) = workouts, cycles
day_id = {s: i for i, s in enumerate(sorted({cell(r, cc, "cycle_start") for r in cr[1:]}))}
with open("whoop_workouts_shared.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["day", "activity", "duration_min", "strain", "max_hr", "avg_hr", "z1", "z2", "z3", "z4", "z5"])
    for r in wr[1:]:
        w.writerow([day_id.get(cell(r, wc, "cycle_start"), ""), cell(r, wc, "activity_name"), minutes(r, wc),
                    *(cell(r, wc, k) for k in ("activity_strain", "max_hr", "avg_hr", "z1", "z2", "z3", "z4", "z5"))])
with open("whoop_days_shared.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["day", "day_strain", "resting_hr", "max_hr", "avg_hr"])
    for r in cr[1:]:
        if cell(r, cc, "day_strain"):
            w.writerow([day_id[cell(r, cc, "cycle_start")], *(cell(r, cc, k) for k in ("day_strain", "resting_hr", "max_hr", "avg_hr"))])
print("wrote whoop_workouts_shared.csv and whoop_days_shared.csv - open them and check before sharing\n")

QUESTIONS = [
    ("Max HR setting in WHOOP", "auto, or manual with the value (e.g. 'manual 196')"),
    ("Custom HR zones in WHOOP", "yes / no / not sure"),
    ("Workouts imported from another device (Strava, a watch, Apple Health)", "yes / no / not sure"),
    ("Strap model", "4.0 / 5.0 / MG, or several"),
    ("Main sports", "optional"),
]
try:
    answers = [input(f"{q} ({hint}): ").strip() or "-" for q, hint in QUESTIONS]
except EOFError:
    answers = ["-"] * len(QUESTIONS)
print("\n--- copy this into your comment on https://github.com/ryanbr/noop/issues/2438 and attach both files ---\n")
print("WHOOP export, shared with whoop_strain_share.py v4.\n")
for (q, _), a in zip(QUESTIONS, answers): print(f"- {q}: {a}")
print("\nIf your NOOP build includes #2459, paste your `effort calib` lines below.")
