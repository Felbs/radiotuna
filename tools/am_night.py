"""am_night.py - Radio Tuna campaign: AM broadcast + the night skywave clock.

At noon the AM band is your local stations; after sunset the ionosphere's
D-layer evaporates and the band fills with skywave from a thousand miles
out - including distant HD Radio (AM IBOC) that is *only* decodable at
certain hours. This tool measures that tide and learns its clock.

One 250 kHz window at a time, five hops cover 530-1700 kHz:
  - carriers on the 10 kHz US raster (count + strength = band openness)
  - IBOC digital sidebands (energy plateaus +-10..15 kHz around a
    carrier = an HD Radio AM station worth chasing with nrsc5)

Appends to lab/am_curve.csv (same clock-learning shape as hf_knob).

Modes:
  scan   - one pass over the whole band: stations, IBOC flags, logging
  curve  - the learned hour-by-hour openness
  watch  - scan every N minutes (leave running into the night)

Example:  python am_night.py scan --antenna "Antenna C"
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hf_knob import (_ensure_sdr_dll_path, open_sdr, grab,      # noqa: E402
                     avg_spectrum_db, FS)

LAB = HERE.parent / "lab"
CURVE = LAB / "am_curve.csv"

HOPS = [630e3, 830e3, 1030e3, 1230e3, 1430e3, 1630e3]   # ±100 kHz used per hop


def scan_hop(iq, center):
    """Carriers on the 10 kHz raster within +-100 kHz; IBOC sideband check."""
    db = avg_spectrum_db(iq, nfft=16384)
    n = len(db)
    binw = FS / n
    c = n // 2
    med = float(np.median(db))
    found = []
    for koff in range(-10, 11):
        f = center + koff * 10e3
        if not (530e3 <= f <= 1705e3):
            continue
        i = c + int(round(koff * 10e3 / binw))
        if not (10 < i < n - 10):
            continue
        pk = float(db[i - 2:i + 3].max() - med)
        if pk < 10.0:
            continue
        # IBOC: flat digital plateaus at +-12 kHz (10..15 kHz shoulders)
        sb = []
        for sgn in (-1, 1):
            a = i + int(sgn * 10e3 / binw)
            b = i + int(sgn * 15e3 / binw)
            lo, hi = min(a, b), max(a, b)
            if 0 < lo and hi < n:
                sb.append(float(np.mean(db[lo:hi]) - med))
        iboc = len(sb) == 2 and min(sb) > 6.0
        found.append({"khz": int(f / 1e3), "snr_db": round(pk, 1),
                      "iboc": iboc})
    return found


def cmd_scan(args):
    from SoapySDR import SOAPY_SDR_RX
    sdr, st = open_sdr(args.antenna)
    import SoapySDR
    now = datetime.now(timezone.utc)
    stations = {}
    print(f"[scan] AM band 530-1700 kHz, {now:%H:%M:%S}Z on {args.antenna}")
    for hop in HOPS:
        sdr.setFrequency(SOAPY_SDR_RX, 0, hop)
        time.sleep(0.15)
        iq = grab(sdr, st, args.dwell)
        for s in scan_hop(iq, hop):
            prev = stations.get(s["khz"])
            if prev is None or s["snr_db"] > prev["snr_db"]:
                stations[s["khz"]] = s
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    sts = sorted(stations.values(), key=lambda s: -s["snr_db"])
    n_iboc = sum(1 for s in sts if s["iboc"])
    print(f"[result] {len(sts)} carriers, {n_iboc} with IBOC (HD Radio AM) sidebands")
    for s in sts[:20]:
        tag = "  << HD (IBOC)" if s["iboc"] else ""
        print(f"    {s['khz']:>4} kHz  +{s['snr_db']:>5} dB{tag}")
    new = not CURVE.exists()
    with open(CURVE, "a", newline="") as fo:
        w = csv.writer(fo)
        if new:
            w.writerow(["ts", "hour_utc", "n_carriers", "n_iboc",
                        "strongest_khz", "strongest_db"])
        top = sts[0] if sts else {"khz": 0, "snr_db": 0}
        w.writerow([now.isoformat(timespec="seconds"), now.hour, len(sts),
                    n_iboc, top["khz"], top["snr_db"]])
    print(f"[scan] logged -> {CURVE.name}   (night vs day tells the skywave story)")
    return sts


def cmd_curve(args):
    if not CURVE.exists():
        print("no data yet - run `scan` (day AND night for the contrast)")
        return
    rows = list(csv.DictReader(open(CURVE)))
    import collections
    acc = collections.defaultdict(list)
    for r in rows:
        acc[int(r["hour_utc"])].append((int(r["n_carriers"]), int(r["n_iboc"])))
    print("hour(UTC)  carriers  HD/IBOC   (skywave floods the band at night)")
    for h in range(24):
        if acc.get(h):
            c = np.mean([a for a, _ in acc[h]])
            i = np.mean([b for _, b in acc[h]])
            print(f"   {h:02d}      {c:5.1f}     {i:4.1f}   " + "#" * int(c))
        else:
            print(f"   {h:02d}        .        .")


def cmd_watch(args):
    print(f"[watch] scanning every {args.every} min - leave running overnight")
    while True:
        try:
            cmd_scan(args)
        except Exception as e:
            print(f"[watch] scan failed ({e}); retrying next cycle")
        time.sleep(args.every * 60)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--antenna", default="Antenna C")
    s.add_argument("--dwell", type=float, default=3.0)
    sub.add_parser("curve")
    w = sub.add_parser("watch")
    w.add_argument("--antenna", default="Antenna C")
    w.add_argument("--dwell", type=float, default=3.0)
    w.add_argument("--every", type=float, default=30)
    args = ap.parse_args()
    if args.cmd == "scan":
        cmd_scan(args)
    elif args.cmd == "curve":
        cmd_curve(args)
    elif args.cmd == "watch":
        cmd_watch(args)


if __name__ == "__main__":
    main()
