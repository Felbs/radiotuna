"""sid_detect.py - Sudden Ionospheric Disturbance (SID) detector.

THE PHYSICS
  Solar X-ray flares ionize the ionospheric D-layer on the DAYLIT
  hemisphere within ~1 light-minute of the flare.  The D-layer is the
  absorbing layer: more ionization -> more collisional absorption of
  radio waves passing through it.  At HF this shows up as a SHORTWAVE
  FADEOUT (SWF) - every skywave signal on the daylit path drops
  together, worst at the low end of the band (absorption ~ f^-2),
  fast onset (minutes) and slow recovery (tens of minutes).

  This tool takes signal-strength time series the rig ALREADY
  recorded, finds daytime absorption excursions, and scores them
  against the NOAA/GOES X-ray flare catalogue.  Zero new hardware.

WHAT IT READS  (--source)
  prop_atlas   lab/prop_atlas.db, table `scans` (the 24/7 observatory)
               ts_utc, band, khz, pwr_db, q, grade  - pwr_db is
               10*log10(mean|y|^2) of a 20 kHz channel slice at FIXED
               receiver gain (prop_atlas.py sets AGC off, rfgain_sel=3,
               IFGR=40), so it is comparable across time.
  dlayer_mw    lab/dlayer_mw_curve.csv, written by lab/am_science_night.py
               epoch,WSM,WLW,WBZ,KMOX = carrier peak dB above the local
               noise floor for four clear-channel MW skywave gauges.
  csv          generic: --csv FILE with a UTC timestamp column and a
               dB-valued column (--time-col/--value-col).

THE METRIC (prop_atlas)
  Per sweep and band, over all channels in the band raster:
      p90  = 90th pct pwr_db  -> the strong-station level
      p10  = 10th pct pwr_db  -> the local noise floor
      sig  = p90 - p10        -> signal ABOVE floor, in dB
  `sig` is the detection statistic.  It is GAIN-INVARIANT: a receiver
  gain change, an antenna swap or an LNA drop moves p90 and p10
  together and cancels.  A real absorption event moves p90 down while
  p10 (mostly receiver/local noise) stays put.  This is the first
  false-positive control and it is not optional.

FALSE-POSITIVE CONTROLS APPLIED
  1. DAYTIME GATE - solar elevation at the observer must exceed
     --min-elev.  A "flare" at local midnight is a false positive by
     construction; the D-layer does not exist at night.
  2. GAIN-INVARIANT STATISTIC - see above.
  3. MW CONTROL BAND - in daytime the MW band reaches us by GROUNDWAVE
     only (the D-layer has already killed MW skywave completely).  So
     a genuine SID must NOT appear on daytime MW.  Any candidate that
     also fires on MW is re-labelled INSTRUMENTAL.
  4. MULTI-BAND CONCURRENCY - a real SWF hits several HF bands in the
     same minute.  --min-bands (default 2) requires that.
  5. MINIMUM DURATION - single-sample excursions are our own noise,
     not the sun.  --min-samples (default 2) rejects them.  The
     single-sample population is still reported, labelled WEAK.
  6. GAP GUARD - samples adjacent to a data gap > 2x the median
     cadence are not allowed to start an event (a restart looks like
     a step).

KNOWN CONFOUNDER THAT NO GATE HERE REMOVES
  prop_atlas's shortwave bands are BROADCAST bands.  The population of
  transmitters keyed up changes on the hour, all day: the EiBi join in
  the same database swings from ~44 to ~262 scheduled channels per
  sweep.  So `sig` on a SW band measures propagation x SCHEDULE, and a
  transmitter line-up change is indistinguishable from an absorption
  change at this cadence.  --schedule-check quantifies it (median
  |delta sig| across an hour boundary vs within an hour).  A clean SID
  watch must use a SINGLE 24/7 carrier (a NDB, WWV, or a VLF MSK
  station), never a broadcast band's aggregate.

GROUND TRUTH
  NOAA/GOES XRS flare summary (`xrsf-l2-flsum`) from the NCEI archive
  covers any past date; SWPC's xrays-7-day.json covers only the last
  week.  --flux-file accepts a user-supplied catalogue instead.  The
  tool NEVER invents a flux series it could not source.

OBSERVER LOCATION
  Never hardcoded (standing dox policy).  Resolution order:
      --lat/--lon  ->  $SID_OBS_LAT/$SID_OBS_LON  ->  JSON at
      $SID_OBS_CONFIG  ->  ./lab_local/observer.json  ->
      ~/.config/sid_observer.json  ->  ~/.sid_observer.json
  JSON form: {"lat": 12.3456, "lon": -65.4321}
  The coordinates are used, never printed and never written to output.

USAGE
  python tools/sid_detect.py                       # full run + verdict
  python tools/sid_detect.py --source dlayer_mw    # the night-only set
  python tools/sid_detect.py --no-fetch            # detector only
  python tools/sid_detect.py --json out.json
"""
from __future__ import annotations

import argparse
import csv as _csv
import json
import math
import os
import sqlite3
import sys
import urllib.request
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LAB = HERE.parent / "lab"
DEFAULT_DB = LAB / "prop_atlas.db"
DEFAULT_MW = LAB / "dlayer_mw_curve.csv"
DEFAULT_CACHE = LAB / "goes_cache"

# NCEI GOES archive: 1-minute flux and the flare-detection summary.
NCEI = ("https://data.ngdc.noaa.gov/platforms/solar-space-observing-"
        "satellites/goes/{sat}/l2/data/{prod}/{yyyy}/{mm}/"
        "dn_{prod}_{sg}_d{ymd}_v2-2-1.nc")
SWPC_7DAY = "https://services.swpc.noaa.gov/json/goes/primary/xrays-7-day.json"
GOES_EPOCH = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Bands prop_atlas sweeps.  MW is the CONTROL, never a detection band.
HF_BANDS = ["49m", "41m", "31m", "25m", "19m"]
CONTROL_BAND = "MW"

# Nominal band centres (MHz) for the f^-2 absorption-ordering check.
BAND_MHZ = {"MW": 1.1, "49m": 6.05, "41m": 7.2, "31m": 9.65,
            "25m": 11.85, "19m": 15.3}


# ───────────────────────────────────────────────────────── observer
def load_observer(args):
    """Resolve observer lat/lon WITHOUT ever hardcoding or echoing it."""
    if args.lat is not None and args.lon is not None:
        return float(args.lat), float(args.lon), "--lat/--lon"
    env_lat, env_lon = os.environ.get("SID_OBS_LAT"), os.environ.get("SID_OBS_LON")
    if env_lat and env_lon:
        return float(env_lat), float(env_lon), "$SID_OBS_LAT/$SID_OBS_LON"
    cands = []
    if os.environ.get("SID_OBS_CONFIG"):
        cands.append(Path(os.environ["SID_OBS_CONFIG"]))
    cands += [HERE.parent / "lab_local" / "observer.json",
              LAB / "lab_local" / "observer.json",
              Path.home() / ".config" / "sid_observer.json",
              Path.home() / ".sid_observer.json"]
    for p in cands:
        try:
            if p.is_file():
                d = json.loads(p.read_text(encoding="utf-8"))
                return float(d["lat"]), float(d["lon"]), str(p.name)
        except Exception:
            continue
    raise SystemExit(
        "sid_detect: no observer location.\n"
        "  SID is a dayside phenomenon, so the daytime gate needs a\n"
        "  latitude/longitude.  Coordinates are NEVER hardcoded here.\n"
        "  Supply one of:\n"
        "    --lat <deg> --lon <deg>\n"
        "    set SID_OBS_LAT / SID_OBS_LON in the environment\n"
        "    a JSON file {\"lat\": .., \"lon\": ..} at $SID_OBS_CONFIG,\n"
        "      lab_local/observer.json, or ~/.config/sid_observer.json\n"
        "  (lab_local/ and ~ are outside the published tree.)")


# ────────────────────────────────────────────────── solar elevation
def solar_elevation(dt_utc, lat_deg, lon_deg):
    """NOAA low-precision solar position (accurate to ~0.01 deg).
    Returns solar elevation in degrees (no refraction correction)."""
    # Julian day / Julian century
    ts = dt_utc.timestamp()
    jd = ts / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0
    # geometric mean longitude / anomaly of the sun
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * mr) * 0.000289)
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    # obliquity
    e0 = (23.0 + (26.0 + ((21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))))
                  / 60.0) / 60.0)
    eps = e0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(app_long)))
    # equation of time (minutes)
    y = math.tan(math.radians(eps / 2.0)) ** 2
    l0r = math.radians(l0)
    eot = 4.0 * math.degrees(
        y * math.sin(2 * l0r) - 2 * 0.016708634 * math.sin(mr)
        + 4 * 0.016708634 * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * 0.016708634 ** 2 * math.sin(2 * mr))
    minutes = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60.0
    true_solar = (minutes + eot + 4.0 * lon_deg) % 1440.0
    ha = true_solar / 4.0 - 180.0
    latr, har = math.radians(lat_deg), math.radians(ha)
    cz = (math.sin(latr) * math.sin(decl)
          + math.cos(latr) * math.cos(decl) * math.cos(har))
    cz = max(-1.0, min(1.0, cz))
    return 90.0 - math.degrees(math.acos(cz))


# ──────────────────────────────────────────────────────── loaders
def _parse_ts(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load_prop_atlas(db_path, lo_pct, hi_pct):
    """-> {band: (times[], sig_db[], p90[], p10[], nchan[])}

    sig = pct(hi) - pct(lo) of pwr_db across the band's channel raster
    at one sweep instant: strong-station level above the local floor."""
    if not Path(db_path).is_file():
        raise SystemExit(f"sid_detect: no such database: {db_path}")
    cx = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    rows = cx.execute(
        "SELECT ts_utc, band, pwr_db FROM scans "
        "WHERE pwr_db IS NOT NULL ORDER BY ts_utc").fetchall()
    cx.close()
    acc = {}
    for ts, band, p in rows:
        acc.setdefault(band, {}).setdefault(ts, []).append(p)
    out = {}
    for band, per_ts in acc.items():
        t, sig, hi, lo, n = [], [], [], [], []
        for ts in sorted(per_ts):
            v = np.asarray(per_ts[ts], float)
            if len(v) < 8:                      # a truncated sweep is not a sweep
                continue
            a = float(np.percentile(v, hi_pct))
            b = float(np.percentile(v, lo_pct))
            t.append(_parse_ts(ts))
            hi.append(a)
            lo.append(b)
            sig.append(a - b)
            n.append(len(v))
        if t:
            out[band] = dict(t=t, sig=np.array(sig), hi=np.array(hi),
                             lo=np.array(lo), n=np.array(n))
    return out


def load_dlayer_mw(path):
    """epoch,WSM,WLW,WBZ,KMOX  (dB above local noise floor)."""
    if not Path(path).is_file():
        raise SystemExit(f"sid_detect: no such file: {path}")
    names = ["MW-650-WSM", "MW-700-WLW", "MW-1030-WBZ", "MW-1120-KMOX"]
    cols = {k: ([], []) for k in names}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) != 5 or not parts[0].lstrip("-").isdigit():
                continue
            ts = datetime.fromtimestamp(int(parts[0]), timezone.utc)
            for k, v in zip(names, parts[1:]):
                cols[k][0].append(ts)
                cols[k][1].append(float(v))
    return {k: dict(t=v[0], sig=np.array(v[1]),
                    hi=np.array(v[1]), lo=np.zeros(len(v[1])),
                    n=np.ones(len(v[1]), int))
            for k, v in cols.items() if v[0]}


def load_generic_csv(path, time_col, value_col, series_col=None):
    with open(path, encoding="utf-8") as f:
        rows = list(_csv.DictReader(f))
    if not rows:
        raise SystemExit(f"sid_detect: {path} has no rows")
    for c in (time_col, value_col):
        if c not in rows[0]:
            raise SystemExit(f"sid_detect: column {c!r} not in {path}; "
                             f"have {list(rows[0])}")
    acc = {}
    for r in rows:
        key = r[series_col] if series_col else "series"
        acc.setdefault(key, ([], []))
        acc[key][0].append(_parse_ts(r[time_col]))
        acc[key][1].append(float(r[value_col]))
    return {k: dict(t=v[0], sig=np.array(v[1]), hi=np.array(v[1]),
                    lo=np.zeros(len(v[1])), n=np.ones(len(v[1]), int))
            for k, v in acc.items()}


# ──────────────────────────────────────────────────────── detector
def _schedule_check(series, det_bands, args):
    """Quantify the broadcast-schedule confounder.

    SW broadcasters change line-up on the hour.  If |delta sig| across an
    hour boundary exceeds |delta sig| within an hour, part of every
    'excursion' is a transmitter schedule change, not the ionosphere."""
    print("\n  schedule confounder (SW bands are BROADCAST bands):")
    bad = []
    for name in det_bands:
        t, v = series[name]["t"], series[name]["sig"]
        cross, same = [], []
        for i in range(len(t) - 1):
            dt = (t[i + 1] - t[i]).total_seconds()
            if dt > 2700:
                continue
            j = abs(v[i + 1] - v[i])
            (cross if t[i].hour != t[i + 1].hour else same).append(j)
        if len(cross) < 5 or len(same) < 5:
            continue
        c, s = float(np.median(cross)), float(np.median(same))
        verdict = "CONTAMINATED" if c > 1.3 * s else "ok"
        if verdict != "ok":
            bad.append(name)
        print(f"    {name:<6} |d sig| across hour {c:5.2f} dB  "
              f"within hour {s:5.2f} dB  -> {verdict}")
    return bad


def rolling_median(times, vals, half_window_min):
    """Time-centred robust baseline; the diurnal curve, not the event."""
    ts = np.array([t.timestamp() for t in times])
    w = half_window_min * 60.0
    out = np.empty(len(vals))
    for i, c in enumerate(ts):
        m = np.abs(ts - c) <= w
        out[i] = np.median(vals[m])
    return out


def mad_sigma(x):
    med = np.median(x)
    mad = np.median(np.abs(x - med))
    return 1.4826 * mad if mad > 0 else float(np.std(x) or 1e-9)


def detect(series, lat, lon, args):
    """Per-series anomaly detection.  Returns (events, diagnostics)."""
    t = series["t"]
    sig = series["sig"]
    elev = np.array([solar_elevation(x, lat, lon) for x in t])
    day = elev >= args.min_elev
    diag = dict(n_total=len(t), n_day=int(day.sum()),
                elev_max=float(elev.max()) if len(elev) else float("nan"))
    if diag["n_day"] < args.min_day_samples:
        diag["skip"] = (f"only {diag['n_day']} daytime samples "
                        f"(need {args.min_day_samples})")
        return [], diag

    dt = [t[i] for i in range(len(t)) if day[i]]
    dv = sig[day]
    secs = np.array([x.timestamp() for x in dt])
    gaps = np.diff(secs)
    cadence = float(np.median(gaps)) if len(gaps) else float("nan")
    diag["cadence_s"] = cadence

    # HEADROOM: you cannot watch a signal fade if there is no signal.
    # If the daytime band already sits at the noise floor, a fadeout has
    # nothing to remove and the series is blind to a SID by construction.
    diag["median_sig_db"] = float(np.median(dv))
    diag["frac_dead"] = float(np.mean(dv < args.min_headroom))
    diag["blind"] = diag["frac_dead"] > 0.5

    base = rolling_median(dt, dv, args.baseline_min)
    resid = dv - base
    sigma = mad_sigma(resid)
    diag["sigma_db"] = float(sigma)
    diag["baseline_db"] = float(np.median(base))

    # gap guard: a sample sitting next to a long gap may be a restart step
    near_gap = np.zeros(len(dt), bool)
    for i in range(len(dt) - 1):
        if secs[i + 1] - secs[i] > 2.0 * cadence:
            near_gap[i] = near_gap[i + 1] = True

    flagged = (np.abs(resid) >= args.nsigma * sigma) & (~near_gap)
    # group consecutive flagged samples (allow one cadence of slack)
    events, i = [], 0
    while i < len(dt):
        if not flagged[i]:
            i += 1
            continue
        j = i
        while (j + 1 < len(dt) and flagged[j + 1]
               and secs[j + 1] - secs[j] <= 1.6 * cadence):
            j += 1
        idx = list(range(i, j + 1))
        k = idx[int(np.argmax(np.abs(resid[idx])))]
        dur = (secs[j] - secs[i]) + cadence      # samples cover a cadence each
        pre = resid[i - 1] if i > 0 else 0.0
        onset = ((resid[k] - pre) / max(cadence / 60.0, 1e-9)
                 if i > 0 else float("nan"))
        events.append(dict(
            t_start=dt[i], t_peak=dt[k], t_end=dt[j],
            n_samples=len(idx),
            resid_db=float(resid[k]), sigma_units=float(resid[k] / sigma),
            value_db=float(dv[k]), baseline_db=float(base[k]),
            duration_min=dur / 60.0,
            onset_db_per_min=float(onset),
            elev_deg=float(elev[day][k]),
            sense="ABSORPTION" if resid[k] < 0 else "ENHANCEMENT"))
        i = j + 1
    diag["n_flagged"] = int(flagged.sum())
    return events, diag


def band_roles(names):
    """Which series can DETECT and which are the control?

    prop_atlas gives us MW for free as a control: in daytime MW arrives
    by groundwave only (the D-layer has already destroyed MW skywave),
    so daytime MW must NOT respond to a flare.  Other sources have no
    such control and the tool says so rather than pretending."""
    det = [n for n in names if n in HF_BANDS]
    ctrl = [n for n in names if n == CONTROL_BAND or n.startswith("MW")]
    if not det:                       # non-prop_atlas source
        det = [n for n in names if n not in ctrl] or list(names)
        ctrl = [n for n in ctrl if n not in det]
    return det, ctrl


def cross_band(per_band, args, cadence_s, det_bands, ctrl_bands):
    """Fuse per-band events into candidates; apply the concurrency and
    MW-control false-positive gates."""
    HF = set(det_bands)
    CTRL = set(ctrl_bands)
    tol = max(cadence_s, 60.0 * args.concurrency_min)
    items = []
    for band, evs in per_band.items():
        for e in evs:
            items.append((band, e))
    items.sort(key=lambda x: x[1]["t_peak"])
    cands, used = [], set()
    for a, (band, ev) in enumerate(items):
        if a in used:
            continue
        grp = [(band, ev)]
        used.add(a)
        for b in range(a + 1, len(items)):
            if b in used:
                continue
            if abs((items[b][1]["t_peak"] - ev["t_peak"]).total_seconds()) <= tol:
                grp.append(items[b])
                used.add(b)
        hf = [g for g in grp if g[0] in HF]
        ctrl = [g for g in grp if g[0] in CTRL]
        if not hf:
            continue
        peak = min(g[1]["t_peak"] for g in hf)
        worst = max(hf, key=lambda g: abs(g[1]["resid_db"]))[1]
        senses = {g[1]["sense"] for g in hf}
        flags = []
        if len(hf) < args.min_bands:
            flags.append(f"SINGLE-BAND({len(hf)})")
        if max(g[1]["n_samples"] for g in hf) < args.min_samples:
            flags.append("SINGLE-SAMPLE")
        if ctrl:
            flags.append("MW-CONTROL-ALSO-FIRED")
        if len(senses) > 1:
            flags.append("MIXED-SENSE")
        if worst["sense"] != "ABSORPTION":
            flags.append("ENHANCEMENT-NOT-FADEOUT")
        # f^-2 ordering: a real SWF bites the LOW bands hardest
        order_ok = None
        if len(hf) >= 2:
            fs = np.array([BAND_MHZ.get(g[0], np.nan) for g in hf])
            rs = np.array([abs(g[1]["resid_db"]) for g in hf])
            good = ~np.isnan(fs)
            if good.sum() >= 2 and len(set(fs[good])) >= 2:
                order_ok = bool(np.corrcoef(fs[good], rs[good])[0, 1] < 0)
                if not order_ok:
                    flags.append("NO-f^-2-ORDERING")
        conf = "STRONG" if not flags else ("WEAK" if len(flags) == 1 else "REJECT")
        cands.append(dict(
            t_peak=peak, t_start=min(g[1]["t_start"] for g in hf),
            t_end=max(g[1]["t_end"] for g in hf),
            bands=sorted(g[0] for g in hf),
            n_bands=len(hf),
            max_samples=max(g[1]["n_samples"] for g in hf),
            depth_db=float(worst["resid_db"]),
            sigma_units=float(worst["sigma_units"]),
            duration_min=float(max(g[1]["duration_min"] for g in hf)),
            onset_db_per_min=float(worst["onset_db_per_min"]),
            elev_deg=float(worst["elev_deg"]),
            sense=worst["sense"], f2_ordering=order_ok,
            flags=flags, confidence=conf))
    cands.sort(key=lambda c: c["t_peak"])
    return cands


# ─────────────────────────────────────────────────────── GOES flux
def _fetch(url, dest, timeout=180):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            dest.write_bytes(r.read())
        return dest
    except Exception as e:
        print(f"  [flux] fetch failed {dest.name}: {e}", file=sys.stderr)
        return None


def goes_flares(dates, cache, sat="goes19", sg="g19"):
    """NOAA/GOES XRS flare-detection summary -> catalogued flares.

    Returns (flares, sources).  A flare is a real catalogue entry with
    start/peak/end times and a class; nothing is interpolated."""
    try:
        import h5py
    except ImportError:
        print("  [flux] h5py missing - cannot read NCEI netCDF; "
              "use --flux-file", file=sys.stderr)
        return [], []
    flares, sources = [], []
    for d in dates:
        ymd = d.strftime("%Y%m%d")
        url = NCEI.format(sat=sat, sg=sg, prod="xrsf-l2-flsum",
                          yyyy=d.strftime("%Y"), mm=d.strftime("%m"), ymd=ymd)
        p = _fetch(url, Path(cache) / f"flsum_{sg}_{ymd}.nc")
        if p is None:
            continue
        sources.append(url)
        try:
            with h5py.File(p, "r") as f:
                tm = f["time"][:]
                st = [s.decode() if isinstance(s, bytes) else str(s)
                      for s in f["status"][:]]
                cl = [s.decode() if isinstance(s, bytes) else str(s)
                      for s in f["flare_class"][:]]
                fid = f["flare_id"][:]
                flx = f["xrsb_flux"][:]
        except Exception as e:
            print(f"  [flux] unreadable {p.name}: {e}", file=sys.stderr)
            continue
        by_id = {}
        for i in range(len(tm)):
            t = GOES_EPOCH + timedelta(seconds=float(tm[i]))
            rec = by_id.setdefault(int(fid[i]), {})
            if st[i] == "EVENT_START":
                rec["start"] = t
            elif st[i] == "EVENT_PEAK":
                rec["peak"] = t
                rec["klass"] = cl[i]
                rec["peak_flux"] = float(flx[i])
            elif st[i] == "EVENT_END":
                rec["end"] = t
        for k, r in by_id.items():
            if "peak" not in r or not r.get("klass"):
                continue
            r["id"] = k
            r.setdefault("start", r["peak"])
            r.setdefault("end", r["peak"])
            flares.append(r)
    flares.sort(key=lambda r: r["peak"])
    return flares, sources


def swpc_recent_flux(cache):
    """SWPC 7-day 1-minute flux - only useful if the data is recent."""
    p = _fetch(SWPC_7DAY, Path(cache) / "xrays-7-day.json")
    if p is None:
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [(_parse_ts(r["time_tag"]), float(r["flux"]))
            for r in d if r.get("energy") == "0.1-0.8nm"]


def load_flux_file(path):
    """User-supplied catalogue: CSV (peak_utc,class[,start_utc,end_utc])
    or the SWPC JSON shape."""
    p = Path(path)
    txt = p.read_text(encoding="utf-8")
    if txt.lstrip().startswith(("[", "{")):
        d = json.loads(txt)
        rows = d if isinstance(d, list) else d.get("flares", [])
        out = []
        for r in rows:
            pk = _parse_ts(r.get("peak") or r.get("peak_utc") or r["time_tag"])
            out.append(dict(peak=pk,
                            start=_parse_ts(r["start"]) if r.get("start") else pk,
                            end=_parse_ts(r["end"]) if r.get("end") else pk,
                            klass=r.get("class") or r.get("klass") or "?",
                            peak_flux=float(r.get("flux", 0) or 0), id=0))
        return sorted(out, key=lambda r: r["peak"])
    rows = list(_csv.DictReader(txt.splitlines()))
    out = []
    for r in rows:
        pk = _parse_ts(r.get("peak_utc") or r.get("peak") or r["time"])
        out.append(dict(peak=pk,
                        start=_parse_ts(r["start_utc"]) if r.get("start_utc") else pk,
                        end=_parse_ts(r["end_utc"]) if r.get("end_utc") else pk,
                        klass=r.get("class", "?"),
                        peak_flux=float(r.get("flux", 0) or 0), id=0))
    return sorted(out, key=lambda r: r["peak"])


def class_rank(k):
    order = {"A": 0, "B": 1, "C": 2, "M": 3, "X": 4}
    return order.get((k or "?")[:1].upper(), -1)


# ───────────────────────────────────────────────────────── matching
def match(cands, flares, tol_min, lat, lon, min_elev, min_class):
    """Two-way matching + the chance base rate."""
    tol = timedelta(minutes=tol_min)
    fl = [f for f in flares if class_rank(f["klass"]) >= class_rank(min_class)]
    # a flare is only DETECTABLE by us if its peak was in our daylight
    for f in fl:
        f["elev"] = solar_elevation(f["peak"], lat, lon)
    matched_c, matched_f = {}, {}
    for i, c in enumerate(cands):
        for j, f in enumerate(fl):
            if f["start"] - tol <= c["t_peak"] <= f["end"] + tol:
                matched_c.setdefault(i, []).append(j)
                matched_f.setdefault(j, []).append(i)
    return fl, matched_c, matched_f


def base_rate(sample_times, flares, tol_min):
    """P(a randomly placed anomaly lands in a flare window) - the number
    that decides whether a 'hit' means anything at all."""
    if not sample_times or not flares:
        return 0.0
    tol = timedelta(minutes=tol_min)
    wins = sorted((f["start"] - tol, f["end"] + tol) for f in flares)
    merged = []
    for a, b in wins:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    hits = 0
    starts = [w[0] for w in merged]
    for t in sample_times:
        k = bisect_right(starts, t) - 1
        if k >= 0 and t <= merged[k][1]:
            hits += 1
    return hits / len(sample_times)


def binom_tail(k, n, p):
    """P(X >= k) for X~Bin(n,p), exact, small n."""
    if n == 0:
        return 1.0
    tot = 0.0
    for i in range(k, n + 1):
        tot += math.comb(n, i) * p ** i * (1 - p) ** (n - i)
    return min(1.0, tot)


# ──────────────────────────────────────────────────────────── main
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="SID / solar-flare detector from recorded RF logs")
    ap.add_argument("--source", default="prop_atlas",
                    choices=["prop_atlas", "dlayer_mw", "csv"])
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--mw-csv", default=str(DEFAULT_MW))
    ap.add_argument("--csv")
    ap.add_argument("--time-col", default="ts")
    ap.add_argument("--value-col", default="peak_db")
    ap.add_argument("--series-col")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--min-elev", type=float, default=10.0,
                    help="solar elevation gate, deg (default 10)")
    ap.add_argument("--nsigma", type=float, default=4.0)
    ap.add_argument("--baseline-min", type=float, default=120.0,
                    help="half-width of the rolling-median baseline, min")
    ap.add_argument("--min-samples", type=int, default=2,
                    help="reject single-sample spikes (our noise, not the sun)")
    ap.add_argument("--min-bands", type=int, default=2)
    ap.add_argument("--min-day-samples", type=int, default=20)
    ap.add_argument("--min-headroom", type=float, default=6.0,
                    help="dB above floor a band needs before a fadeout "
                         "could even be seen (default 6)")
    ap.add_argument("--concurrency-min", type=float, default=15.0)
    ap.add_argument("--lo-pct", type=float, default=10.0)
    ap.add_argument("--hi-pct", type=float, default=90.0)
    ap.add_argument("--tol-min", type=float, default=30.0,
                    help="candidate<->flare match tolerance, min")
    ap.add_argument("--min-class", default="C",
                    help="smallest flare class counted as ground truth")
    ap.add_argument("--sid-timescale-min", type=float, default=10.0,
                    help="the timescale a real SID lives on")
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--flux-file", help="user-supplied flare catalogue")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--schedule-check", action="store_true", default=True,
                    help="quantify the broadcast-schedule confounder")
    ap.add_argument("--no-schedule-check", dest="schedule_check",
                    action="store_false")
    ap.add_argument("--json", help="write the full result as JSON")
    args = ap.parse_args(argv)

    lat, lon, src = load_observer(args)
    print("=" * 72)
    print("SID DETECTOR - solar flares from already-recorded RF logs")
    print("=" * 72)
    print(f"observer: loaded from {src} (coordinates not echoed)")

    # ---------------------------------------------------------- load
    if args.source == "prop_atlas":
        series = load_prop_atlas(args.db, args.lo_pct, args.hi_pct)
        what = (f"{args.db}\n            metric = p{args.hi_pct:.0f} - "
                f"p{args.lo_pct:.0f} of channel pwr_db (dB above floor, "
                f"gain-invariant)")
    elif args.source == "dlayer_mw":
        series = load_dlayer_mw(args.mw_csv)
        what = f"{args.mw_csv}\n            metric = carrier dB above noise floor"
    else:
        if not args.csv:
            raise SystemExit("--source csv needs --csv FILE")
        series = load_generic_csv(args.csv, args.time_col, args.value_col,
                                  args.series_col)
        what = f"{args.csv} [{args.value_col}]"
    if not series:
        raise SystemExit("sid_detect: no usable series in the source")
    print(f"source:   {what}")

    all_t = sorted(t for s in series.values() for t in s["t"])
    print(f"span:     {all_t[0]:%Y-%m-%d %H:%M}Z -> {all_t[-1]:%Y-%m-%d %H:%M}Z "
          f"({(all_t[-1] - all_t[0]).days + 1} days)")
    print(f"series:   {len(series)}  -> {', '.join(sorted(series))}")

    # ------------------------------------------------------- detect
    print("\n--- PER-SERIES DETECTION " + "-" * 47)
    per_band, diags, cadences, day_times = {}, {}, [], []
    for name in sorted(series):
        evs, dg = detect(series[name], lat, lon, args)
        diags[name] = dg
        if "skip" in dg:
            print(f"  {name:<14} {dg['n_total']:>4} samples  SKIPPED: {dg['skip']}"
                  f"  (max solar elev {dg['elev_max']:.1f} deg)")
            continue
        cadences.append(dg["cadence_s"])
        per_band[name] = evs
        print(f"  {name:<14} {dg['n_total']:>4} samples, {dg['n_day']:>4} daytime,"
              f" cadence {dg['cadence_s']/60:.0f} min,"
              f" sigma {dg['sigma_db']:.2f} dB -> {len(evs)} excursion(s)")
        print(f"  {'':14} headroom: median {dg['median_sig_db']:.1f} dB above "
              f"floor, {dg['frac_dead']*100:.0f}% of daytime sweeps below "
              f"{args.min_headroom:.0f} dB"
              f"{'   <-- BLIND' if dg['blind'] else ''}")
    if not per_band:
        print("\nVERDICT: NEGATIVE - no series survived the daytime gate.")
        print("  A SID is a DAYSIDE phenomenon.  This data has no usable")
        print("  daytime coverage, so it cannot support flare detection.")
        return 2

    cadence_s = float(np.median(cadences))
    resolvable = cadence_s <= args.sid_timescale_min * 60.0
    print(f"\n  median cadence {cadence_s/60:.1f} min vs SID timescale "
          f"{args.sid_timescale_min:.0f} min -> "
          f"{'RESOLVABLE' if resolvable else 'UNDER-SAMPLED'}")
    if not resolvable:
        print("  !! At this cadence the onset-rate / slow-recovery SIGNATURE")
        print("     cannot be measured: a whole SID fits between two samples.")
        print("     The detector degrades to a bare excursion test, which is")
        print("     exactly the statistic we are supposed to distrust.")

    det_bands, ctrl_bands = band_roles(sorted(per_band))
    print(f"  detection series: {', '.join(det_bands) or 'NONE'}")
    if ctrl_bands:
        print(f"  control series  : {', '.join(ctrl_bands)}  "
              f"(daytime groundwave-only -> must NOT respond to a flare)")
    else:
        print("  control series  : NONE - the MW groundwave control is not")
        print("                    available for this source, so control #3")
        print("                    is NOT applied and confidence is capped.")

    day_times = []
    for name in det_bands:
        day_times += [t for t in series[name]["t"]
                      if solar_elevation(t, lat, lon) >= args.min_elev]
    day_times = sorted(set(day_times))

    contaminated = []
    if args.schedule_check and args.source == "prop_atlas":
        contaminated = _schedule_check(series, det_bands, args) or []

    cands = cross_band(per_band, args, cadence_s, det_bands, ctrl_bands)
    strong = [c for c in cands if c["confidence"] == "STRONG"]
    weak = [c for c in cands if c["confidence"] == "WEAK"]
    print(f"\n--- CANDIDATES (after false-positive gates) " + "-" * 28)
    print(f"  {len(cands)} fused candidate(s): "
          f"{len(strong)} STRONG, {len(weak)} WEAK, "
          f"{len(cands)-len(strong)-len(weak)} REJECT")
    if cands:
        print(f"  {'peak UTC':<18}{'bands':<6}{'depth':>8}{'sig':>7}"
              f"{'dur':>7}{'onset':>9}{'elev':>7}  flags")
        for c in cands:
            print(f"  {c['t_peak']:%Y-%m-%d %H:%M}  {c['n_bands']:<4}"
                  f"{c['depth_db']:>+8.2f}{c['sigma_units']:>+7.1f}"
                  f"{c['duration_min']:>6.0f}m{c['onset_db_per_min']:>+9.3f}"
                  f"{c['elev_deg']:>6.1f}  "
                  f"{c['confidence']}{(' ' + ','.join(c['flags'])) if c['flags'] else ''}")

    # -------------------------------------------------- ground truth
    print("\n--- GROUND TRUTH (NOAA/GOES X-ray) " + "-" * 37)
    dates = sorted({t.date() for t in all_t})
    flares, sources = [], []
    if args.flux_file:
        flares = load_flux_file(args.flux_file)
        sources = [f"user file: {args.flux_file}"]
        print(f"  user-supplied catalogue: {len(flares)} flare(s)")
    elif args.no_fetch:
        print("  --no-fetch: no ground truth loaded.  NO correlation is")
        print("  claimed.  Re-run without --no-fetch or pass --flux-file.")
    else:
        recent = swpc_recent_flux(args.cache_dir)
        if recent:
            lo, hi = recent[0][0], recent[-1][0]
            covers = lo.date() <= dates[0] and hi.date() >= dates[-1]
            print(f"  SWPC xrays-7-day.json covers "
                  f"{lo:%Y-%m-%d}..{hi:%Y-%m-%d} - "
                  f"{'covers our data' if covers else 'DOES NOT cover our data'}")
        print(f"  falling back to the NCEI GOES archive for "
              f"{dates[0]}..{dates[-1]} ({len(dates)} day files)")
        flares, sources = goes_flares([datetime(d.year, d.month, d.day)
                                       for d in dates], args.cache_dir)
        print(f"  NCEI xrsf-l2-flsum: {len(flares)} catalogued flare(s)")
    if not flares:
        print("\nVERDICT: INCONCLUSIVE - candidates found but NO sourced")
        print("  X-ray ground truth was loaded, so no correlation can be")
        print("  claimed.  Supply --flux-file or restore network access.")
        return 3

    fl, mc, mf = match(cands, flares, args.tol_min, lat, lon,
                       args.min_elev, args.min_class)
    by_class = {}
    for f in fl:
        by_class[f["klass"][:1]] = by_class.get(f["klass"][:1], 0) + 1
    dayfl = [f for f in fl if f["elev"] >= args.min_elev]
    print(f"  >= {args.min_class}-class in window: {len(fl)} "
          f"({', '.join(f'{k}:{v}' for k, v in sorted(by_class.items()))})")
    print(f"  of those, peaking in OUR daylight (elev >= {args.min_elev:.0f}"
          f" deg): {len(dayfl)}")
    biggest = max(fl, key=lambda f: (class_rank(f["klass"]),
                                     float(f["klass"][1:] or 0)))
    print(f"  largest flare in window: {biggest['klass']} at "
          f"{biggest['peak']:%Y-%m-%d %H:%M}Z "
          f"(observer solar elev {biggest['elev']:+.1f} deg)")

    # ---------------------------------------------- contingency table
    print("\n--- CONTINGENCY " + "-" * 56)
    det = [j for j in range(len(fl)) if j in mf
           and fl[j]["elev"] >= args.min_elev]
    missed = [j for j in range(len(fl)) if j not in mf
              and fl[j]["elev"] >= args.min_elev]
    hit = [i for i in mc]
    fa = [i for i in range(len(cands)) if i not in mc]
    print(f"  candidates matching a flare      : {len(hit)} / {len(cands)}")
    print(f"  candidates with NO flare (false alarms): {len(fa)}")
    print(f"  daylight flares we DETECTED      : {len(det)} / {len(dayfl)}")
    print(f"  daylight flares we MISSED        : {len(missed)} / {len(dayfl)}")
    if det:
        print("  detected:")
        for j in det:
            print(f"    {fl[j]['klass']:<6} {fl[j]['peak']:%Y-%m-%d %H:%M}Z"
                  f"  elev {fl[j]['elev']:5.1f}")
    if missed:
        show = sorted(missed, key=lambda j: -class_rank(fl[j]["klass"]))[:12]
        print(f"  missed (top {len(show)} by class):")
        for j in show:
            print(f"    {fl[j]['klass']:<6} {fl[j]['peak']:%Y-%m-%d %H:%M}Z"
                  f"  elev {fl[j]['elev']:5.1f}")

    # --------------------------------------------------- base rate
    p0 = base_rate(day_times, fl, args.tol_min)
    pval = binom_tail(len(hit), max(len(cands), 1), p0) if cands else 1.0
    print("\n--- BASE RATE (the honesty check) " + "-" * 38)
    print(f"  daytime sample instants considered: {len(day_times)}")
    print(f"  fraction of daytime time inside a +/-{args.tol_min:.0f} min flare"
          f" window: {p0*100:.1f}%")
    print(f"  => a RANDOM anomaly matches a flare {p0*100:.1f}% of the time.")
    print(f"  observed {len(hit)}/{len(cands)} matched; "
          f"P(>= that many by chance) = {pval:.3f}")
    if p0 > 0.25:
        print("  !! The flare rate is so high that coincidence is the DEFAULT")
        print("     outcome.  A match here is not evidence of causation.")

    # ----------------------------------------------------- verdict
    print("\n--- VERDICT " + "-" * 60)
    blind = [n for n in det_bands if diags[n].get("blind")]
    verdict = _verdict(args, cands, strong, fl, dayfl, det, p0, pval,
                       cadence_s, resolvable, contaminated, ctrl_bands,
                       blind, det_bands)
    for line in verdict:
        print("  " + line)

    if args.json:
        Path(args.json).write_text(json.dumps(dict(
            source=args.source, span=[all_t[0].isoformat(), all_t[-1].isoformat()],
            cadence_s=cadence_s, resolvable=resolvable,
            diagnostics={k: {kk: (vv if not isinstance(vv, float)
                                  else round(vv, 4))
                             for kk, vv in v.items()} for k, v in diags.items()},
            candidates=[{**c, "t_peak": c["t_peak"].isoformat(),
                         "t_start": c["t_start"].isoformat(),
                         "t_end": c["t_end"].isoformat()} for c in cands],
            flares=[{"class": f["klass"], "peak": f["peak"].isoformat(),
                     "elev": round(f["elev"], 2)} for f in fl],
            base_rate=p0, p_value=pval, sources=sources,
            verdict=verdict), indent=2), encoding="utf-8")
        print(f"\n  wrote {args.json}")
    return 0


def _verdict(args, cands, strong, fl, dayfl, det, p0, pval, cadence_s,
             resolvable, contaminated=(), ctrl_bands=(), blind=(),
             det_bands=()):
    v = []
    big = [f for f in dayfl if class_rank(f["klass"]) >= 3]   # M or X
    if blind:
        v.append(f"BLIND BANDS: {', '.join(blind)} spend most of local daylight "
                 f"within")
        v.append(f"  {args.min_headroom:.0f} dB of the noise floor - there is no "
                 f"skywave signal there")
        v.append("  to fade.  A fadeout cannot be observed on a band that is "
                 "already")
        v.append("  at the floor, so those series carry no SID information at "
                 "all.")
        if len(blind) == len(det_bands):
            v.append("  EVERY detection band is blind: this dataset is "
                     "structurally")
            v.append("  incapable of showing a shortwave fadeout.")
    if contaminated:
        v.append(f"CONFOUNDED: {', '.join(contaminated)} jump more across an "
                 f"hour boundary than")
        v.append("  within one.  These are BROADCAST bands; the transmitter "
                 "line-up")
        v.append("  changes on the hour, so the metric is propagation x "
                 "SCHEDULE.")
        v.append("  A SID watch needs ONE always-on carrier, not a band "
                 "aggregate.")
    if not ctrl_bands:
        v.append("NO CONTROL SERIES: the daytime-MW groundwave control was not "
                 "available,")
        v.append("  so instrumental excursions cannot be separated from "
                 "ionospheric ones.")
    if not resolvable:
        v.append(f"UNDER-SAMPLED: {cadence_s/60:.0f} min cadence cannot resolve a "
                 f"SID's fast onset /")
        v.append("  slow recovery.  Every 'signature' test in this tool is "
                 "therefore")
        v.append("  disabled by the data itself, not by choice.")
    if len(big) == 0:
        v.append(f"NO DRIVER: the window contains {len(dayfl)} daylight flare(s) "
                 f">= {args.min_class}, but")
        v.append("  ZERO of M-class or above in our daylight.  Sub-M flares do "
                 "not")
        v.append("  reliably produce a measurable shortwave fadeout at "
                 "mid-latitudes,")
        v.append("  so the expected true-positive count is ~0.  Absence of "
                 "detections")
        v.append("  is the PREDICTED result, not a failure of the detector.")
    elif len(big) < 3:
        v.append(f"UNDERPOWERED: only {len(big)} M/X-class flare(s) peaked in our "
                 f"daylight.")
        v.append("  n<3 supports an anecdote, never a correlation.")
    if p0 > 0.25:
        v.append(f"CHANCE-DOMINATED: {p0*100:.0f}% of daytime already sits inside "
                 f"a flare window,")
        v.append("  so a coincidence is more likely than not.  p="
                 f"{pval:.3f} is not significance.")
    if strong and big and p0 <= 0.25 and pval < 0.05:
        v.append(f"POSITIVE (tentative): {len(strong)} STRONG candidate(s), "
                 f"{len(det)} flare(s) matched,")
        v.append(f"  p={pval:.3f} against the base rate.  Verify with a "
                 f"higher-cadence run.")
    else:
        v.append("NEGATIVE for flare detection on this dataset.  The existing "
                 "logs")
        v.append("  do not support a SID claim.  What they DO establish is the ")
        v.append("  instrument's noise floor: sigma of the daytime absorption "
                 "metric,")
        v.append("  measured above, is the sensitivity a future SID watch must "
                 "beat.")
    v.append("")
    v.append("TO MAKE THIS WORK, in the order the blockers actually bite:")
    v.append("  1. SIGNAL - pick ONE transmitter that is loud here all day and")
    v.append("     always keyed: a VLF/LF MSK station, a 24/7 NDB, or WWV. "
             "A band")
    v.append("     aggregate that sits at the noise floor can never show a "
             "fadeout.")
    v.append("  2. CADENCE - sample it at <= 1 min so onset and recovery are")
    v.append("     measurable, not inferred.")
    v.append("  3. PATIENCE - then wait for M-class flares; sub-M events will "
             "not")
    v.append("     move a mid-latitude path reliably, and n<3 is still an "
             "anecdote.")
    v.append("  The receiver and this code are not the limit; the observable "
             "is.")
    return v


if __name__ == "__main__":
    sys.exit(main())
