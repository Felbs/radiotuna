"""prop_atlas.py - Radio Tuna Propagation Observatory (phases 1-2).

The 24/7 AM+SW band-health atlas: every 30 minutes, one 4-second gulp
per band (MW + the round-the-clock SW broadcast bands), channelized by
whole_band's ka9q-style survey into a quality grade for EVERY channel,
stored durably in SQLite. For shortwave rows the EiBi schedule is
joined at insert time, so each row carries who SHOULD have been on
that channel at that minute - the heard-vs-scheduled propagation score
lives in the data from day one.

Radio etiquette (the laws):
  * single-tenant SDR via radio_lock ONLY, priority 20 (background) -
    the atlas yields to everything and skips a cycle rather than block;
  * ~30 s of radio time per sweep; the ~10 min of CPU-side channelizing
    happens AFTER the radio is released;
  * one sweep per interval, never retry-hammered.

  python tools/prop_atlas.py once             # single sweep (testing)
  python tools/prop_atlas.py run              # foreground loop
  python tools/prop_atlas.py status           # last sweep + row counts
  tools/atlas_start.ps1                       # detached 24/7 daemon
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import radio_lock
import whole_band

LAB = HERE.parent / "lab"
DB = LAB / "prop_atlas.db"
EIBI = LAB / "eibi_sked.csv"
LOG = LAB / "prop_atlas_log.txt"
TMP = LAB / "atlas_tmp"

FS = 2.048e6
SECS = 4.0
ALIVE_Q = 18                 # whole_band's "listenable" line
PRIORITY = 20                # background: yields to EVERYTHING
OWNER = "prop_atlas"

# (band, capture-center kHz, raster start, stop, step kHz)
# Centers sit 2.5 kHz OFF the channel raster so the SDR's own DC spike
# parks BETWEEN channels (whole_band's rule; MW's 1115 does the same).
BANDS = [
    ("MW",  1115.0,   530,  1700, 10),
    ("49m", 6052.5,  5900,  6200,  5),
    ("41m", 7202.5,  7200,  7450,  5),
    ("31m", 9652.5,  9400,  9900,  5),
    ("25m", 11852.5, 11600, 12100, 5),
    ("19m", 15302.5, 15100, 15800, 5),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans(
    ts_utc   TEXT NOT NULL,
    band     TEXT NOT NULL,
    khz      REAL NOT NULL,
    pwr_db   REAL,
    q        INTEGER,
    grade    TEXT,
    expected TEXT);
CREATE TABLE IF NOT EXISTS sweeps(
    ts_utc     TEXT NOT NULL,
    band       TEXT NOT NULL,
    n_channels INTEGER,
    n_alive    INTEGER);
CREATE INDEX IF NOT EXISTS idx_scans_khz_ts ON scans(khz, ts_utc);
CREATE INDEX IF NOT EXISTS idx_scans_ts    ON scans(ts_utc);
CREATE INDEX IF NOT EXISTS idx_sweeps_ts   ON sweeps(ts_utc);
CREATE VIEW IF NOT EXISTS hourly AS
    SELECT khz, CAST(substr(ts_utc, 12, 2) AS INTEGER) AS hour_utc,
           AVG(q) AS avg_q, COUNT(*) AS n
    FROM scans GROUP BY khz, hour_utc;
"""


def log(msg):
    line = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}"
    print(f"[atlas] {line}", flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def db_open():
    cx = sqlite3.connect(DB, timeout=10.0)
    cx.execute("PRAGMA journal_mode=WAL")   # panel reads while we write
    cx.executescript(SCHEMA)
    return cx


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- EiBi
_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def _day_ok(days, wd):
    """Best-effort EiBi Days field. Blank = daily; handle Mo / Mo-Fr /
    Mo,We,Fr; anything odd (dates like '2Jul') counts as a match - the
    atlas would rather over-expect than silently drop a station."""
    d = days.strip()
    if not d:
        return True
    if d in _DAYS:
        return d == _DAYS[wd]
    if "-" in d:
        p = d.split("-", 1)
        if p[0] in _DAYS and p[1] in _DAYS:
            i0, i1 = _DAYS.index(p[0]), _DAYS.index(p[1])
            return (i0 <= wd <= i1) if i0 <= i1 else (wd >= i0 or wd <= i1)
    if "," in d:
        parts = [p.strip() for p in d.split(",")]
        if all(p in _DAYS for p in parts):
            return _DAYS[wd] in parts
    return True


def load_eibi():
    """EiBi rows -> bucketed index: int(kHz) -> [(khz, t0m, t1m, days,
    station)]. Buckets are padded so a +/-2.5 kHz raster lookup only
    touches one key. Missing file -> empty index (SW rows get NULL)."""
    idx = {}
    try:
        lines = EIBI.read_text(encoding="latin-1").splitlines()
    except OSError:
        return idx
    for ln in lines[1:]:
        f = ln.split(";")
        if len(f) < 5:
            continue
        try:
            khz = float(f[0])
        except ValueError:
            continue
        t0, t1 = 0, 1440
        try:
            a, b = f[1].split("-")
            t0 = int(a[:2]) * 60 + int(a[2:4])
            t1 = int(b[:2]) * 60 + int(b[2:4])
        except (ValueError, IndexError):
            pass
        ent = (khz, t0, t1, f[2], f[4].strip())
        k = int(round(khz))
        for kk in (k - 2, k - 1, k, k + 1, k + 2):
            idx.setdefault(kk, []).append(ent)
    return idx


def eibi_expected(idx, chan_khz, when, tol=2.5):
    """Who SHOULD be on this channel at this UTC moment (or None).
    Time spans are wrap-around aware (2200-0300)."""
    m, wd = when.hour * 60 + when.minute, when.weekday()
    names = []
    for khz, t0, t1, days, station in idx.get(int(round(chan_khz)), []):
        if abs(khz - chan_khz) > tol:
            continue
        on = (t0 <= m < t1) if t0 <= t1 else (m >= t0 or m < t1)
        if on and station and _day_ok(days, wd):
            if station not in names:
                names.append(station)
    return " / ".join(names[:2]) if names else None


# ------------------------------------------------------------- capture
def _open_sdr():
    """Warden-polite open, drm_sweep-style: enumerate -> open by the
    ENUMERATED kwargs. A few gentle tries, then give up (skip cycle)."""
    import SoapySDR
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    for attempt in range(3):
        devs = SoapySDR.Device.enumerate("driver=sdrplay")
        if devs:
            try:
                return SoapySDR.Device(devs[0])
            except RuntimeError:
                pass
        if attempt < 2:
            time.sleep(5)
    return None


def capture_all():
    """One warden-held pass: 4 s cf32 gulp per band to TMP. Returns
    [(band, center_khz, start, stop, step, path)] - possibly short if a
    higher-priority holder asked for the radio mid-sweep. None = the
    radio was unavailable and the cycle should be skipped."""
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
    TMP.mkdir(exist_ok=True)
    got = []
    with radio_lock.Holder(OWNER, "band-health atlas sweep", PRIORITY,
                           wait_s=20) as h:
        if not h.ok:
            st = radio_lock.status() or {}
            log(f"radio busy ({st.get('owner', '?')} "
                f"p{st.get('priority', '?')}) - skipping cycle")
            return None
        sdr = _open_sdr()
        if sdr is None:
            log("SDR would not open - skipping cycle "
                "(if 'no match' persists: Restart-Service SDRplayAPIService)")
            return None
        try:
            sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
            try:
                sdr.setAntenna(SOAPY_SDR_RX, 0,
                               os.environ.get("RT_AM_ANTENNA", "Antenna A"))
                sdr.setGainMode(SOAPY_SDR_RX, 0, False)
                sdr.writeSetting("biasT_ctrl", "false")
                sdr.writeSetting("rfgain_sel", "3")
                sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40)
            except Exception:
                pass
            n = int(FS * SECS)
            chunk = np.empty(131072, np.complex64)
            for band, ckhz, lo, hi, step in BANDS:
                why = radio_lock.should_yield()
                if why:
                    log(f"yielding radio mid-sweep: {why}")
                    break
                sdr.setFrequency(SOAPY_SDR_RX, 0, ckhz * 1e3)
                time.sleep(0.4)
                st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
                sdr.activateStream(st)
                time.sleep(0.2)
                buf = np.empty(n, np.complex64)
                filled, t0 = 0, time.time()
                while filled < n and time.time() - t0 < SECS + 6:
                    r = sdr.readStream(st, [chunk], len(chunk),
                                       timeoutUs=int(1e6))
                    if r.ret > 0:
                        take = min(r.ret, n - filled)
                        buf[filled:filled + take] = chunk[:take]
                        filled += take
                sdr.deactivateStream(st)
                sdr.closeStream(st)
                radio_lock.heartbeat()
                if filled < n * 0.9:
                    log(f"{band}: short read ({filled}/{n}) - dropped")
                    continue
                p = TMP / f"gulp_{band}.cf32"
                buf.tofile(p)
                got.append((band, ckhz, lo, hi, step, p))
        finally:
            del sdr
    return got


# ------------------------------------------------------------- process
def process_and_store(gulps, ts, eibi, when=None):
    """CPU side, AFTER the radio is released: channelize each gulp with
    whole_band.survey, join EiBi for SW rows, insert. `when` is the
    CAPTURE moment for the schedule join (defaults to now - pass it
    when back-filling an old gulp)."""
    when = when or datetime.now(timezone.utc)
    cx = db_open()
    totals = {}
    try:
        for band, ckhz, lo, hi, step, path in gulps:
            x = np.fromfile(path, dtype=np.complex64)
            centers = [k * 1e3 for k in range(lo, hi + 1, step)]
            t0 = time.time()
            rows = whole_band.survey(x, FS, ckhz * 1e3, centers)
            sw = band != "MW"
            recs = []
            for r in rows:
                exp = (eibi_expected(eibi, r["khz"], when, tol=step / 2.0)
                       if sw else None)
                recs.append((ts, band, r["khz"], r.get("pwr_db"),
                             r.get("q"), r.get("grade"), exp))
            alive = sum(1 for r in rows if (r.get("q") or 0) >= ALIVE_Q)
            cx.executemany(
                "INSERT INTO scans VALUES(?,?,?,?,?,?,?)", recs)
            cx.execute("INSERT INTO sweeps VALUES(?,?,?,?)",
                       (ts, band, len(rows), alive))
            cx.commit()
            totals[band] = alive
            log(f"{band}: {len(rows)} ch, {alive} alive "
                f"({time.time() - t0:.0f}s cpu)")
            try:
                path.unlink()
            except OSError:
                pass
    finally:
        cx.close()
    return totals


def sweep_once():
    ts = _ts()
    eibi = load_eibi()
    if not eibi:
        log("note: eibi_sked.csv missing/empty - SW 'expected' will be NULL")
    gulps = capture_all()
    if not gulps:
        return None
    log(f"radio released; channelizing {len(gulps)} band(s)...")
    totals = process_and_store(gulps, ts, eibi)
    log(f"sweep {ts} done: " +
        " ".join(f"{b}:{n}" for b, n in totals.items()))
    return totals


# ----------------------------------------------------------------- CLI
def cmd_run(interval_min):
    log(f"observatory up: every {interval_min} min, priority {PRIORITY}")
    while True:
        t0 = time.time()
        try:
            sweep_once()
        except Exception as e:                    # a bad cycle never
            log(f"sweep error: {e!r}")            # kills the metronome
        left = interval_min * 60 - (time.time() - t0)
        while left > 0:
            time.sleep(min(left, 60))
            left = interval_min * 60 - (time.time() - t0)


def cmd_status():
    if not DB.exists():
        print("no atlas db yet - run `prop_atlas.py once` first")
        return 1
    cx = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True,
                         timeout=5.0)
    try:
        n_scans = cx.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
        n_sweeps = cx.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0]
        last = cx.execute("SELECT MAX(ts_utc) FROM sweeps").fetchone()[0]
        print(f"scans rows {n_scans}, sweep rows {n_sweeps}, "
              f"last sweep {last}")
        if last:
            for b, nc, na in cx.execute(
                    "SELECT band, n_channels, n_alive FROM sweeps "
                    "WHERE ts_utc=?", (last,)):
                print(f"  {b:4} {na:3}/{nc} alive")
        day = (datetime.now(timezone.utc) - timedelta(days=1)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
        print("top channels, last 24 h:")
        for khz, band, q, grade, exp in cx.execute(
                "SELECT khz, band, MAX(q), grade, expected FROM scans "
                "WHERE ts_utc >= ? AND q IS NOT NULL "
                "GROUP BY khz ORDER BY 3 DESC LIMIT 10", (day,)):
            print(f"  {khz:7.0f} kHz {band:4} Q{q:>3} {grade or '':9} "
                  f"{exp or ''}")
    finally:
        cx.close()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="foreground loop (daemon via "
                       "tools/atlas_start.ps1)")
    p.add_argument("--interval-min", type=float, default=30.0)
    sub.add_parser("once", help="single sweep now (testing)")
    sub.add_parser("status", help="last sweep + row counts")
    args = ap.parse_args()
    if args.cmd == "run":
        cmd_run(args.interval_min)
    elif args.cmd == "once":
        sys.exit(0 if sweep_once() is not None else 1)
    else:
        sys.exit(cmd_status())


if __name__ == "__main__":
    main()
