"""band_stress.py - Radio Tuna: lock stress test over every scanned
AM + shortwave carrier.

For each station in lab/am_stations.json + lab/sw_stations.json:
tune (offset), grab a few seconds, run the am_best chain, and grade:

  LOCK       carrier prominence clears the dial floor and the chain
             produced sane program audio
  WEAK       carrier present but marginal (fady, co-channel-crushed)
  NO-LOCK    nothing usable on this channel right now

Writes lab/stress_report.csv (one row per channel) and prints a
summary with failure taxonomy - the honest answer to "can we actually
lock these or do improvements need to be made."

  python band_stress.py [--secs 4] [--limit N]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hf_knob import open_sdr, grab, FS
import am_best

LAB = HERE.parent / "lab"
OFFSET = 30e3


def grade(diag, level):
    csnr = diag["carrier_snr_db"]
    if csnr >= 25 and level > 0.02:
        return "LOCK"
    if csnr >= 14:
        return "WEAK"
    return "NO-LOCK"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=4.0)
    ap.add_argument("--limit", type=int, default=0,
                    help="test only the strongest N per deck (0 = all)")
    args = ap.parse_args()

    jobs = []
    for deck, fname in (("am", "am_stations.json"), ("sw", "sw_stations.json")):
        try:
            sts = json.loads((LAB / fname).read_text())
        except (OSError, ValueError):
            sts = []
        sts = sorted(sts, key=lambda s: -s["db"])
        if args.limit:
            sts = sts[:args.limit]
        jobs += [(deck, s) for s in sts]
    if not jobs:
        print("no scanned stations found - run panel scans first")
        return 1

    print(f"[stress] {len(jobs)} channels x {args.secs:.0f}s "
          f"(~{len(jobs)*(args.secs+1.2)/60:.0f} min)")
    sdr, st = open_sdr("Antenna A")
    import SoapySDR
    from scipy.signal import resample_poly

    rows = []
    t_start = time.time()
    try:
        for i, (deck, s) in enumerate(jobs):
            khz = s["khz"]
            sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, khz * 1e3 - OFFSET)
            time.sleep(0.25)
            _ = grab(sdr, st, 0.3)          # flush the retune transient
            iq = grab(sdr, st, args.secs)
            n = np.arange(len(iq), dtype=np.float64)
            x = iq * np.exp(-2j * np.pi * OFFSET / FS * n)
            x = resample_poly(x, 2, 25).astype(np.complex64)
            x = x - np.mean(x)
            try:
                audio, diag = am_best.best_chunk(x, 20_000)
                level = float(np.sqrt(np.mean(audio ** 2)))
                verdict = grade(diag, level)
            except Exception as e:
                diag, level, verdict = {"carrier_snr_db": 0, "cochannel": 0,
                                        "fade_frac6": 0, "cutoff_hz": 0,
                                        "hets_hz": []}, 0.0, f"ERROR:{e}"
            rows.append({
                "deck": deck, "khz": khz, "scan_db": s["db"],
                "id": s.get("id") or "", "verdict": verdict,
                "carrier_db": diag["carrier_snr_db"],
                "cochannel": diag["cochannel"],
                "fade_pct": round(diag["fade_frac6"] * 100, 1),
                "bw_hz": diag["cutoff_hz"], "hets": len(diag["hets_hz"]),
                "audio_rms": round(level, 3)})
            done, total = i + 1, len(jobs)
            eta = (time.time() - t_start) / done * (total - done)
            print(f"  [{done}/{total}] {deck} {khz:7.0f}  "
                  f"{verdict:8} carrier {diag['carrier_snr_db']:5.1f} dB  "
                  f"co-ch {diag['cochannel']}  (~{eta/60:.0f} min left)",
                  flush=True)
    finally:
        try:
            sdr.deactivateStream(st); sdr.closeStream(st)
        except Exception:
            pass

    out = LAB / "stress_report.csv"
    with open(out, "w", encoding="utf-8") as f:
        cols = list(rows[0].keys())
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(str(r[c]).replace(",", ";") for c in cols) + "\n")

    print("\n" + "=" * 62)
    for deck in ("am", "sw"):
        d = [r for r in rows if r["deck"] == deck]
        if not d:
            continue
        n = len(d)
        lock = sum(r["verdict"] == "LOCK" for r in d)
        weak = sum(r["verdict"] == "WEAK" for r in d)
        no = sum(r["verdict"] == "NO-LOCK" for r in d)
        print(f"{deck.upper():3} {n:4} tested: {lock} LOCK ({100*lock/n:.0f}%) "
              f"{weak} WEAK ({100*weak/n:.0f}%)  {no} NO-LOCK ({100*no/n:.0f}%)")
        # failure taxonomy on the non-locks
        bad = [r for r in d if r["verdict"] != "LOCK"]
        gone = sum(r["carrier_db"] < 14 for r in bad)
        fady = sum(r["carrier_db"] >= 14 and r["fade_pct"] > 8 for r in bad)
        crowd = sum(r["carrier_db"] >= 14 and r["cochannel"] >= 3 for r in bad)
        print(f"     non-locks: {gone} carrier-gone (band closed/daytimer off) "
              f"{fady} fading  {crowd} co-channel pileup")
    print(f"report: {out}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
