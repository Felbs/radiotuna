"""hd_quality.py - Radio Tuna: the HD Radio quality meter + gain lab.

The scientifically-honest HD dial, three layers deep:
  - nrsc5's own MER (dB, lower/upper sideband) - the constellation truth
  - BER - the bit-level truth
  - AUDIO SECONDS DECODED per capture second - the product truth
    (DATAMOSH law: the user hears audio, not MER; a capture that syncs
    but emits 2 s of audio from 15 s is a 13% station, whatever the MER)

Modes:
  measure  - run nrsc5 over a captured .cu8, report the three dials
  sweep    - one station x a gain grid: capture short IQ at each gain,
             measure, print the curve, save the winner to
             lab/hd_gain_cal.json  (the TV GAINS-table, reborn)
  corpus   - the big sample: every HD station x the gain grid ->
             lab/hd_corpus/ + measurements ledger (replay forever)

Examples:
  python hd_quality.py sweep --mhz 88.5
  python hd_quality.py corpus --mhz-list 88.5,89.3,90.9,91.9,93.3
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
CORPUS = LAB / "hd_corpus"
CORPUS.mkdir(exist_ok=True)
CAL = LAB / "hd_gain_cal.json"
LEDGER = LAB / "hd_quality.jsonl"

import hd_radio
from hd_radio import NRSC5, FS_NRSC5


def measure(cu8_path, prog=0, quiet=True):
    """nrsc5 over a capture -> the three dials."""
    wav = Path(str(cu8_path) + ".wav")
    if wav.exists():
        wav.unlink()
    cmd = [NRSC5, "-r", str(cu8_path), "-o", str(wav), str(prog)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace")
    mers = []
    bers = []
    sync = 0
    lost = 0
    station = None
    for line in p.stdout:
        if not quiet:
            print("   " + line.rstrip(), flush=True)
        if "Synchronized" in line:
            sync += 1
        if "Lost synchronization" in line:
            lost += 1
        if "MER:" in line:
            try:
                seg = line.split("MER:")[1]
                lo = float(seg.split("dB")[0])
                hi = float(seg.split("dB")[1].split(",")[-1].strip()
                           .split()[-1]) if seg.count("dB") > 1 else lo
                mers.append((lo + hi) / 2)
            except (ValueError, IndexError):
                pass
        if "BER:" in line:
            try:
                bers.append(float(line.split("BER:")[1].split(",")[0]))
            except (ValueError, IndexError):
                pass
        if "Station name:" in line:
            station = line.split("Station name:")[1].strip()
    p.wait()
    cap_bytes = Path(cu8_path).stat().st_size
    cap_secs = cap_bytes / (2 * FS_NRSC5)          # cu8: 2 bytes/sample
    audio_secs = 0.0
    if wav.exists():
        audio_secs = max(0, (wav.stat().st_size - 44)) / (44100 * 2 * 2)
        wav.unlink()                                # corpus keeps IQ, not wav
    return {"sync": sync > 0, "lost": lost,
            "mer_db": round(sum(mers)/len(mers), 2) if mers else None,
            "mer_n": len(mers),
            "ber": round(sum(bers)/len(bers), 6) if bers else None,
            "audio_ratio": round(audio_secs / max(cap_secs, 0.1), 3),
            "cap_secs": round(cap_secs, 1), "station": station}


def _capture(mhz, secs, ifgr, rfgain):
    class A:
        pass
    a = A()
    a.mhz = mhz; a.secs = secs; a.ifgr = ifgr; a.rfgain = str(rfgain)
    return hd_radio.cmd_capture(a)


def _score(m):
    """One number to rank gains: audio is king, MER breaks ties."""
    if m["mer_db"] is None:
        return -1.0
    return m["audio_ratio"] * 100 + m["mer_db"]


def cmd_sweep(args):
    gains = [(float(i), r) for i in args.ifgrs.split(",")
             for r in args.rfgains.split(",")]
    rows = []
    print(f"[sweep] {args.mhz} MHz x {len(gains)} gain points "
          f"({args.secs:.0f}s each)")
    for ifgr, rf in gains:
        cap = _capture(args.mhz, args.secs, ifgr, rf)
        m = measure(cap, args.prog)
        m.update({"mhz": args.mhz, "ifgr": ifgr, "rfgain": rf,
                  "file": str(cap), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
        rows.append(m)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(m) + "\n")
        print(f"  IFGR {ifgr:>4.0f} rf {rf}: MER {str(m['mer_db']):>6} dB  "
              f"audio {m['audio_ratio']*100:5.1f}%  "
              f"sync={m['sync']} lost={m['lost']}  {m['station'] or ''}")
    best = max(rows, key=_score)
    print(f"[sweep] WINNER: IFGR {best['ifgr']:.0f} rfgain {best['rfgain']} "
          f"(MER {best['mer_db']}, audio {best['audio_ratio']*100:.0f}%)")
    cal = {}
    if CAL.exists():
        cal = json.loads(CAL.read_text())
    cal[f"{args.mhz:.1f}"] = {"ifgr": best["ifgr"], "rfgain": best["rfgain"],
                              "mer_db": best["mer_db"],
                              "audio_ratio": best["audio_ratio"],
                              "ts": best["ts"]}
    CAL.write_text(json.dumps(cal, indent=1))
    print(f"[sweep] calibration saved -> {CAL}")
    return rows


def cmd_corpus(args):
    mhzs = [float(m) for m in args.mhz_list.split(",")]
    print(f"[corpus] {len(mhzs)} stations x gain grid - the big HD sample")
    for mhz in mhzs:
        args.mhz = mhz
        try:
            cmd_sweep(args)
        except Exception as e:
            print(f"  {mhz}: failed ({str(e)[:60]})")
    print(f"[corpus] ledger: {LEDGER}")
    print(f"[corpus] calibration table: {CAL}")


def cmd_measure(args):
    m = measure(args.file, args.prog, quiet=args.quiet)
    print(json.dumps(m, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--file", required=True)
    m.add_argument("--prog", type=int, default=0)
    m.add_argument("--quiet", action="store_true", default=True)
    s = sub.add_parser("sweep")
    s.add_argument("--mhz", type=float, required=True)
    s.add_argument("--secs", type=float, default=12)
    s.add_argument("--prog", type=int, default=0)
    s.add_argument("--ifgrs", default="30,40,50")
    s.add_argument("--rfgains", default="3,5")
    c = sub.add_parser("corpus")
    c.add_argument("--mhz-list", default="88.5,89.3,90.9,91.9,93.3,99.7,107.5")
    c.add_argument("--secs", type=float, default=12)
    c.add_argument("--prog", type=int, default=0)
    c.add_argument("--ifgrs", default="30,40,50")
    c.add_argument("--rfgains", default="3,5")
    args = ap.parse_args()
    {"measure": cmd_measure, "sweep": cmd_sweep, "corpus": cmd_corpus}[args.cmd](args)


if __name__ == "__main__":
    main()
