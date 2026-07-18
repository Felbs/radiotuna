"""hf_knob.py - Radio Tuna campaign: the Knob of Time, aimed at the ionosphere.

HF (3-30 MHz) is the band where WHEN matters more than WHERE: the same
frequency is dead at noon and worldwide at midnight. Nobody's receiver
learns that for *your* antenna at *your* location - so this tool does.

Two kinds of probes, one learned clock:
  - FT8 watering holes (3.573, 7.074, 14.074, 21.074 ... MHz): thousands
    of stations transmit every 15 s, worldwide. The amount of narrowband
    activity in each 3 kHz window is a live, free ionosonde.
  - Shortwave BROADCAST bands (49m/41m/31m/25m/19m/16m): count AM
    carriers on the 5 kHz raster = how many stations your antenna hears.

Every sweep appends to lab/hf_curve.csv; `curve` renders the learned
hour-by-hour openness per band - your personal ionosphere model, built
from your own roof. (The Knob of Time from the TV campaign, on HF.)

Modes:
  sweep  - one pass over all probes (~90 s), print + log scores
  curve  - render the learned hour curves from the log
  watch  - sweep every N minutes forever (the knob learns while you sleep)

Example:  python hf_knob.py sweep --antenna "Antenna C"
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
LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
CURVE = LAB / "hf_curve.csv"

FS = 250_000.0

FT8 = [("FT8-80m", 3.573e6), ("FT8-40m", 7.074e6), ("FT8-30m", 10.136e6),
       ("FT8-20m", 14.074e6), ("FT8-17m", 18.100e6), ("FT8-15m", 21.074e6),
       ("FT8-10m", 28.074e6)]
SWBC = [("SW-120m", 2.40e6), ("SW-90m", 3.30e6), ("SW-60m", 4.90e6),
        ("SW-49m", 6.00e6), ("SW-41m", 7.30e6), ("SW-31m", 9.65e6),
        ("SW-25m", 11.85e6), ("SW-22m", 13.72e6), ("SW-19m", 15.40e6),
        ("SW-16m", 17.70e6), ("SW-13m", 21.65e6), ("SW-11m", 25.85e6)]


def _ensure_sdr_dll_path():
    if os.name != "nt":
        return
    root = Path(sys.executable).resolve().parent
    for p in (root / "Library" / "bin",
              Path(r"C:\Program Files\SDRplay\API\x64"),
              Path(r"C:\Program Files\SDRplay\API")):
        if p.is_dir():
            os.environ["PATH"] = str(p) + os.pathsep + os.environ["PATH"]
            try:
                os.add_dll_directory(str(p))
            except Exception:
                pass


_ensure_sdr_dll_path()


def open_sdr(antenna):
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    try:
        sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    except Exception:
        pass
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 30)
        sdr.writeSetting("rfgain_sel", "0")
    except Exception:
        pass
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    sdr.activateStream(st)
    return sdr, st


def grab(sdr, st, secs):
    n_want = int(secs * FS)
    buf = np.empty(2 * 65536, np.int16)
    out = np.empty(2 * n_want, np.int16)
    got = 0
    while got < n_want:
        r = sdr.readStream(st, [buf], 65536, timeoutUs=1_000_000)
        if r.ret > 0:
            n = min(r.ret, n_want - got)
            out[2 * got:2 * (got + n)] = buf[:2 * n]
            got += n
        elif r.ret < 0 and r.ret != -1:
            break
    return ((out[0::2].astype(np.float32) + 1j * out[1::2].astype(np.float32))
            / 32768.0).astype(np.complex64)[:got]


def avg_spectrum_db(iq, nfft=8192):
    seg = iq[:len(iq) // nfft * nfft].reshape(-1, nfft)
    seg = seg * np.hanning(nfft).astype(np.float32)
    P = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
    return 10 * np.log10(P + 1e-12)


def ft8_activity(iq):
    """Occupancy of the 0..+3 kHz USB window: # of 30 Hz bins > floor+6dB."""
    db = avg_spectrum_db(iq)
    n = len(db)
    binw = FS / n
    c = n // 2
    med = float(np.median(db))
    a, b = c + int(200 / binw), c + int(3000 / binw)
    return int(np.sum(db[a:b] > med + 6.0)), round(float(np.max(db[a:b]) - med), 1)


def sw_carriers(iq):
    """AM broadcast carriers on the 5 kHz raster within the 250k window."""
    db = avg_spectrum_db(iq)
    n = len(db)
    binw = FS / n
    c = n // 2
    med = float(np.median(db))
    cnt = 0
    strongest = 0.0
    for koff in range(-24, 25):
        f = koff * 5000.0
        i = c + int(round(f / binw))
        if 2 < i < n - 3 and abs(koff) > 0:
            pk = float(db[i - 1:i + 2].max() - med)
            if pk > 8.0:
                cnt += 1
                strongest = max(strongest, pk)
    return cnt, round(strongest, 1)


def cmd_sweep(args, sdr_st=None):
    own = sdr_st is None
    if own:
        sdr, st = open_sdr(args.antenna)
    else:
        sdr, st = sdr_st
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX
    now = datetime.now(timezone.utc)
    rows = []
    print(f"[sweep] {now:%H:%M:%S}Z on {args.antenna}")
    for name, f in FT8 + SWBC:
        sdr.setFrequency(SOAPY_SDR_RX, 0, f)
        time.sleep(0.15)
        iq = grab(sdr, st, args.dwell)
        if name.startswith("FT8"):
            score, peak = ft8_activity(iq)
        else:
            score, peak = sw_carriers(iq)
        rows.append((name, score, peak))
        bar = "#" * min(40, score)
        print(f"  {name:<8} {f/1e6:7.3f} MHz  score {score:>3}  peak +{peak:>5} dB  {bar}")
    if own:
        sdr.deactivateStream(st)
        sdr.closeStream(st)
    new = not CURVE.exists()
    with open(CURVE, "a", newline="") as fo:
        w = csv.writer(fo)
        if new:
            w.writerow(["ts", "hour_utc", "band", "score", "peak_db"])
        for name, score, peak in rows:
            w.writerow([now.isoformat(timespec="seconds"), now.hour, name, score, peak])
    print(f"[sweep] logged {len(rows)} rows -> {CURVE.name}")
    return rows


def cmd_curve(args):
    if not CURVE.exists():
        print("no data yet - run `sweep` a few times (or `watch` overnight)")
        return
    import collections
    acc = collections.defaultdict(list)
    with open(CURVE) as fi:
        for row in csv.DictReader(fi):
            acc[(row["band"], int(row["hour_utc"]))].append(float(row["score"]))
    bands = sorted({b for b, _ in acc})
    print("learned hour-curves (UTC hours, mean score; blank = no data yet)")
    print(f"{'band':<9}" + "".join(f"{h:>4}" for h in range(24)))
    for b in bands:
        line = f"{b:<9}"
        for h in range(24):
            v = acc.get((b, h))
            line += f"{np.mean(v):4.0f}" if v else "   ."
        print(line)


def cmd_watch(args):
    print(f"[watch] sweeping every {args.every} min - Ctrl+C to stop")
    while True:
        try:
            cmd_sweep(args)
        except Exception as e:
            print(f"[watch] sweep failed ({e}); retrying next cycle")
        time.sleep(args.every * 60)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweep")
    s.add_argument("--antenna", default="Antenna C")
    s.add_argument("--dwell", type=float, default=4.0)
    sub.add_parser("curve")
    w = sub.add_parser("watch")
    w.add_argument("--antenna", default="Antenna C")
    w.add_argument("--dwell", type=float, default=4.0)
    w.add_argument("--every", type=float, default=20)
    args = ap.parse_args()
    if args.cmd == "sweep":
        cmd_sweep(args)
    elif args.cmd == "curve":
        cmd_curve(args)
    elif args.cmd == "watch":
        cmd_watch(args)


if __name__ == "__main__":
    main()
