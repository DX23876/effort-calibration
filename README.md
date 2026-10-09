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
  `python3 effort_calibration.py DATA_DIR [--variants]` reads one folder per contributor
  (`whoop_workouts_shared.csv`, `whoop_days_shared.csv`, optional `info.txt` with the comment text) and prints a
  Markdown report.
- `test_effort_calibration.py`: `python3 -m unittest test_effort_calibration` (34 tests).
- `dev/xcheck_scipy.py`: checks that the dependency-free optimiser finds the same minimum as scipy (needs scipy).

## Method

- **Model:** every minute is weighted by e^(β·(x−55)/10), where x is its intensity in % of heart-rate reserve.
  Inside zones 1 to 5 that is the zone's midpoint. For the time below zone 1 it is that time's own intensity,
  estimated from the workout's average heart rate, and the weight switches off below 20 % of reserve (WHOOP scores
  workouts that stay close to resting heart rate as 0). Score = 21·b / (b⁸ + 21⁸)^(1/8) with b = a·ln(1 + load/k),
  so 21 is approached but never reached. Fitted: a, k and β.
- **Training rows:** only workouts in WHOOP's current zone definition (% of reserve) train the fit. Older workouts
  are converted and scored, so they still check it, but no fitted number depends on converting them through an
  estimated max heart rate.
- **Loss:** Huber, with every WHOOP band (light / moderate / high / all-out) carrying the same total weight, so the
  few hard workouts are not drowned out by the many light ones.
- **Left out:** strength sessions by name (WHOOP adds muscular load to Strength Trainer sessions; `Bodybuilding`
  included), incomplete rows, custom zones without zone bounds, and rows that need a resting or average heart rate
  the export does not have. Nothing missing is ever substituted.
- **Generic names** (`Activity`, `Other` and their translations) are kept and checked: the report compares their
  residuals with the same contributor's named workouts and flags anyone whose generic workouts sit more than a point
  above. An export with no activity names at all is marked *cardio-unverified*: it is scored but never trains a fit.
- **Custom zones** apply from the month given ("since"). Before that, WHOOP's own zones are used.
- **WHOOP's zone change:** older exports define zones as % of max heart rate, newer ones as % of heart-rate
  reserve, and the switch did not happen on the same date for every account. It is found per contributor from the
  data (a workout's average heart rate against what its zone shares imply under either definition). For wearers on
  WHOOP's automatic max heart rate, the second-highest daily maximum over the trailing 90 days stands in for it.
  That is a stand-in only: WHOOP starts from Gellish's age formula and raises it from workout peaks, and the
  exports cannot pin its value down.
- **Validation:** each contributor is scored by a fit made without their data (leave-one-out). The new curve has
  to beat today's formula for every contributor on error, with band agreement at least as high as today. Rows whose
  training set is small are flagged. With a single contributor, the older half of the days is used to fit and the
  newer half to score, as a method check only.
- **Every report** shows, per contributor, the average WHOOP, today and new score of their hard and light workouts,
  and checks band weighting on and off, the cap exponent at 6, 8 and 10, the parameters of each leave-one-out fit and
  a bootstrap limited to the workouts at 14+.
- **`--variants`** adds a leave-one-out table of every model compared in the issue: Edwards weights with only the
  curve refitted, Banister weights, the model published on 2026-09-26 (one weight for all time below zone 1, cap 6),
  the floor at 20 or 30 %, caps 6, 8 and 10, and every contributor weighted equally. Slow: one leave-one-out run
  per variant.

The page and the script find columns by header name (English, German, Spanish, French, Portuguese), fall back
to the current export layout for other app languages, accept exports without an activity name column and skip the
`__MACOSX` entries that macOS Finder adds when it re-zips.

NOOP is not affiliated with WHOOP. WHOOP is named only to describe the export this tool reads.
