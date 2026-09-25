# Effort calibration: share your WHOOP data

A small tool for the Effort recalibration discussed in [ryanbr/noop#2438](https://github.com/ryanbr/noop/issues/2438).
It turns a WHOOP data export into two small CSV files that keep only what the fit needs, and fits a
recalibrated Effort curve from everyone's files.

**Use it in your browser:** https://dx23876.github.io/effort-calibration/

Or run the same logic as a script: `python3 whoop_strain_share.py my_whoop_data.zip`

## Privacy

- The page runs entirely in your browser. Nothing is uploaded: it has no server, and its
  Content-Security-Policy blocks every network request. It also works saved and opened offline.
- **Kept:** year and month, activity name, duration, strain, max/avg heart rate, HR-zone shares; per day:
  Day Strain, resting/max/avg heart rate. Days are numbered 0, 1, 2… instead of dated.
- **Dropped:** exact dates and times, sleep, HRV, recovery, skin temperature, SpO₂ and the journal.
- Year and month stay because WHOOP changed how it defines its heart-rate zones at some point (see below).
- Nothing is shared unless you attach the files to the issue yourself. That comment is public.

## Files

- `index.html`: the browser version, a single file with no dependencies.
- `whoop_strain_share.py`: the Python version. It produces byte-identical files.
- `effort_calibration.py`: the fit. Pure standard library.
  `python3 effort_calibration.py DATA_DIR` reads one folder per contributor
  (`whoop_workouts_shared.csv`, `whoop_days_shared.csv`, optional `info.txt` with the comment text) and prints a
  Markdown report.
- `test_effort_calibration.py`: `python3 -m unittest test_effort_calibration`.
- `dev/xcheck_scipy.py`: checks that the dependency-free optimiser finds the same minimum as scipy (needs scipy).

## Method

- **Model:** load = minutes × (share below zone 1 × w0 + Σ zone share × e^(β·(x−55)/10)), where x is the zone's
  intensity in % of heart-rate reserve. Score = 21·b / (b⁶ + 21⁶)^(1/6) with b = a·ln(1 + load/k), so 21 is
  approached but never reached. Fitted: a, k, β and w0 (0 ≤ w0 ≤ 1).
- **Loss:** Huber, with every WHOOP band (light / moderate / high / all-out) carrying the same total weight, so the
  few hard workouts are not drowned out by the many light ones.
- **Left out:** strength sessions (WHOOP adds muscular load to their strain), incomplete rows, and workouts with
  custom zones but no zone bounds. Custom zones with bounds are placed at their real intensity.
- **WHOOP's zone change:** older exports define zones as % of max heart rate, newer ones as % of heart-rate
  reserve. The switch is found per contributor from the data (a workout's average heart rate against what its
  zone shares imply under either definition), and older workouts are moved to their real intensity. WHOOP's max
  heart rate is taken as the second-highest daily maximum over the trailing 90 days, which matched WHOOP's own
  zones far better than one value for all years.
- **Validation:** each contributor is scored by a fit made without their data (leave-one-out). The new curve has
  to beat today's formula for every contributor, and put more workouts in WHOOP's band. With a single
  contributor, the older half of the days is used to fit and the newer half to score, as a method check only.

Both find columns by header name (English, German, Spanish, French, Portuguese) and fall back to the
current export layout for other app languages.

NOOP is not affiliated with WHOOP. WHOOP is named only to describe the export this tool reads.
