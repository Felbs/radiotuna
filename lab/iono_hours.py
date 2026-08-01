"""First ionosphere measurement from our own atlas: the band-door hour map.

THE IDEA. prop_atlas has been grading every AM/SW channel alive-or-static
around the clock since 7/28 — 4 days, 6 bands, ~55k scans. That time series
IS an ionospheric instrument: which bands are open at which hours is set by
the D layer (absorption, dies at night, ∝1/f² per dlayer-diary) and the F
layer (MUF, holds high bands open by day and lets them go after midnight).
If the physics we indexed this week is right, the hour-of-day structure of
our own data is PREDICTABLE — so predict it first, then look.

epistemic:
  type: hypothesis
  tier: measured
  prediction: |
    P1 MW (~1 MHz): strongly night-favored. Absorption ∝ 1/f² makes MW the
       D layer's biggest victim — skywave dead by day, floods in at night.
       Expect the alive count to step up within ~1 h of local sunset
       (~00:20Z this week) and collapse within ~1 h of sunrise (~10:10Z).
    P2 49m (5.9-6.2 MHz): night-favored, same mechanism, gentler ratio
       (absorption falls as 1/f²) — call it night/day >= 2x, less than MW's.
    P3 19m (15.1-15.8 MHz): INVERTED — day/evening open, deep-night dead,
       because 15 MHz needs the F layer's MUF, which sags after midnight.
    P4 31m (9.4-9.9 MHz): intermediate — evening-peaked, neither extreme.
    P5 the 20:16Z evening opening we watched live on 7/31 (19m/25m coming
       alive) is a repeatable daily feature, not weather — it should appear
       at a similar hour on the other days.
  test: this file, offline, sweeps table only. Predictions written before
        any query was run against hour-of-day structure.
  result: |
    RAN 2026-07-31 ~21:00 local, 598 band-sweeps over 5 days.
    P1 direction CONFIRMED (MW night 23.0 vs day 14.0) but its implied
       magnitude ordering WRONG: MW's night/day ratio (x1.6) is the
       SMALLEST of the night bands, not the largest — 49m x7.4, 41m x3.2,
       31m x3.0. Cause found in the data: MW's day floor (~14 alive) is
       LOCAL GROUNDWAVE, which absorption never touches. n_alive mixes two
       propagation modes; the skywave signal is the DELTA over the day
       floor (+9), not the ratio.
    P2 CONFIRMED, stronger than predicted: 49m x7.4 night (>= 2x predicted),
       and its 12-13Z collapse to 0.4 is the D layer switching on at local
       morning, right on cue.
    P3 CONFIRMED, clean inversion: 19m x0.1 — day 6.2 alive, deep night
       ~0.3. The MUF door in our own data.
    P4 CONFIRMED: 31m intermediate (x3.0), peaking 00-02Z = local evening.
    P5 CONFIRMED repeatable: the 19-23Z high-band opening appears all four
       full days; its exact hour wanders (19Z on 7/28, 21-22Z on 7/29-31) —
       day-to-day propagation variance, worth tracking as a metric.
    UNPREDICTED: 25m is FLAT (x1.2) — the crossover band, absorbed by day
       AND MUF-limited at night. Nobody predicted it; the data taught it.
  conclusion: |
    The atlas IS an ionospheric instrument: 1/f^2 absorption and the MUF
    door are both visible in 5 days of our own alive-counts, and one model
    error (groundwave floor) was caught and explained. Standing confound:
    broadcaster schedules correlate with these hours by design — the sharp
    49m/19m inversion argues physics, but a schedule-aware rerun (EiBI
    join: alive/scheduled instead of alive) is the honest next rung.
  next: |
    (1) normalize by EiBI schedule -> alive/scheduled per band-hour kills
        the sign-off confound (ionoTuna #38's first real metric);
    (2) day-to-day opening-hour drift as a solar/geomag proxy — grade
        against NOAA K-index (already in the corpus);
    (3) MW delta-over-floor (not ratio) as the D-layer gauge, feeding
        dlayer-diary's absorption curve with 24/7 automated data.

Honest limits, stated up front: 4 days is a tiny sample; broadcasters have
SCHEDULES (a band can look "closed" because transmitters signed off, not
because the ionosphere shut the door — EiBI-scheduled sign-offs correlate
with propagation on purpose, which partially confounds P1-P4); and n_alive
is bottom-of-funnel (one strong station keeps a band "alive"). This is a
first look with eyes open, not a calibrated foF2 instrument.
"""
import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path(r"Z:\src\gr-radiotuna\lab\prop_atlas.db")
BANDS = ["MW", "49m", "41m", "31m", "25m", "19m"]     # low to high frequency

c = sqlite3.connect(DB)
rows = c.execute("select ts_utc, band, n_alive from sweeps").fetchall()
print(f"  {len(rows)} band-sweeps, "
      f"{c.execute('select count(distinct substr(ts_utc,1,10)) from sweeps').fetchone()[0]} days\n")

# hour-of-day aggregation
agg = defaultdict(list)
for ts, band, alive in rows:
    agg[(band, int(ts[11:13]))].append(alive)

print("  mean alive stations by UTC hour  (. = no data, local = UTC-4)")
print(f"  {'band':<5}" + "".join(f"{h:>4}" for h in range(24)))
for band in BANDS:
    cells = []
    for h in range(24):
        v = agg.get((band, h))
        cells.append(f"{sum(v)/len(v):>4.1f}" if v else "   .")
    print(f"  {band:<5}" + "".join(cells))

print("\n  night/day contrast (night = 01-09Z, day = 14-22Z):")
for band in BANDS:
    night = [x for h in range(1, 10) for x in agg.get((band, h), [])]
    day = [x for h in range(14, 23) for x in agg.get((band, h), [])]
    if night and day:
        n, d = sum(night) / len(night), sum(day) / len(day)
        ratio = (n / d) if d > 0.05 else float("inf")
        lean = "NIGHT" if n > d * 1.3 else ("DAY" if d > n * 1.3 else "flat")
        print(f"    {band:<5} night {n:5.2f}  day {d:5.2f}  -> {lean}"
              f"  (x{ratio:.1f})" if ratio != float("inf") else
              f"    {band:<5} night {n:5.2f}  day {d:5.2f}  -> NIGHT (day ~0)")

print("\n  P5 — the daily evening opening (19m+25m alive, hours 19-23Z by day):")
by_day = defaultdict(lambda: defaultdict(float))
for ts, band, alive in rows:
    if band in ("19m", "25m") and 19 <= int(ts[11:13]) <= 23:
        by_day[ts[:10]][int(ts[11:13])] += alive
for day in sorted(by_day):
    hrs = by_day[day]
    line = "".join(f"{hrs.get(h, 0):>5.0f}" for h in range(19, 24))
    print(f"    {day}  hrs 19-23Z:{line}")
