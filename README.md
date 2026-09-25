# Effort calibration: share your WHOOP data

A small tool for the Effort recalibration discussed in [ryanbr/noop#2438](https://github.com/ryanbr/noop/issues/2438).
It turns a WHOOP data export into two small CSV files that keep only what the fit needs.

**Use it in your browser:** https://dx23876.github.io/effort-calibration/

Or run the same logic as a script: `python3 whoop_strain_share.py my_whoop_data.zip`

## Privacy

- The page runs entirely in your browser. Nothing is uploaded: it has no server, and its
  Content-Security-Policy blocks every network request. It also works saved and opened offline.
- **Kept:** activity name, duration, strain, max/avg heart rate, HR-zone shares; per day: Day Strain,
  resting/max/avg heart rate. Days are numbered 0, 1, 2… instead of dated.
- **Dropped:** all dates and times, sleep, HRV, recovery, skin temperature, SpO₂ and the journal.
- Nothing is shared unless you attach the files to the issue yourself. That comment is public.

## Files

- `index.html`: the browser version, a single file with no dependencies.
- `whoop_strain_share.py`: the Python version. It produces byte-identical files.

Both find columns by header name (English, German, Spanish, French, Portuguese) and fall back to the
current export layout for other app languages.

NOOP is not affiliated with WHOOP. WHOOP is named only to describe the export this tool reads.
