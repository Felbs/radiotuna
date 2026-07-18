"""bandscan.py - Radio Tuna: the legality-aware band identifier.

Point it anywhere 25-1500 MHz. It IDENTIFIES what a signal is from the US
band plan and enforces one hard rule in code:

  * Viewing RF energy (power, waterfall) is legal for ANYTHING.
  * DECODING is gated by policy:
      decode    - openly-transmitted, meant to be received (broadcast,
                  ham, ADS-B, AIS, weather, air traffic) -> tools may run
      identify  - legal to hear but we only label it, no payload tooling
      view-only - private/encrypted (cellular, cordless, paging,
                  encrypted public-safety) -> POWER/WATERFALL ONLY,
                  decoders are refused BY THIS MODULE, on purpose.

`may_decode(mhz)` is the guard other tools import before touching a
signal. This is the "see the cell-phone waterfall but never decode it"
principle, enforced in code, not just intention.

Modes:
  selftest - classification + the guard-refuses-view-only test
  where    - classify a single frequency (MHz)
  survey   - list every carrier in a capture with its policy label
"""
import argparse
import sys
from pathlib import Path

import numpy as np

# (lo_MHz, hi_MHz, name, policy)  - US allocations, coarse but honest
BANDPLAN = [
    (0.53, 1.70, "AM broadcast", "decode"),
    (1.80, 2.00, "160m ham", "decode"),
    (2.30, 2.50, "120m SW broadcast", "decode"),
    (3.50, 4.00, "80m ham", "decode"),
    (4.75, 5.06, "60m SW broadcast", "decode"),
    (5.33, 5.41, "60m ham (channels)", "decode"),
    (5.90, 6.20, "49m SW broadcast", "decode"),
    (7.00, 7.30, "40m ham / SW", "decode"),
    (9.40, 9.90, "31m SW broadcast", "decode"),
    (10.10, 10.15, "30m ham", "decode"),
    (11.60, 12.10, "25m SW broadcast", "decode"),
    (13.57, 13.87, "22m SW broadcast", "decode"),
    (14.00, 14.35, "20m ham", "decode"),
    (15.10, 15.80, "19m SW broadcast", "decode"),
    (17.48, 17.90, "16m SW broadcast", "decode"),
    (18.06, 18.17, "17m ham", "decode"),
    (21.00, 21.45, "15m ham", "decode"),
    (21.45, 21.85, "13m SW broadcast", "decode"),
    (24.89, 24.99, "12m ham", "decode"),
    (26.96, 27.41, "CB (27 MHz)", "decode"),
    (28.00, 29.70, "10m ham", "decode"),
    (30.0, 50.0, "VHF low / land mobile", "identify"),
    (50.0, 54.0, "6m ham", "decode"),
    (88.0, 108.0, "FM broadcast (+ RDS)", "decode"),
    (108.0, 118.0, "aviation nav (VOR/ILS)", "identify"),
    (118.0, 137.0, "air band (ATC voice, AM)", "decode"),
    (137.0, 138.0, "weather/space satellites", "decode"),
    (144.0, 148.0, "2m ham (+ APRS 144.39)", "decode"),
    (148.0, 150.8, "gov/military land mobile", "view-only"),
    (150.8, 156.0, "VHF land mobile / business", "identify"),
    (156.0, 162.0, "marine VHF / AIS", "decode"),
    (162.4, 162.55, "NOAA weather radio", "decode"),
    (400.0, 406.0, "radiosondes / sat", "decode"),
    (406.0, 406.1, "EMERGENCY BEACONS (EPIRB/PLB)", "view-only"),
    (420.0, 450.0, "70cm ham", "decode"),
    (450.0, 470.0, "UHF business / public safety", "identify"),
    (470.0, 512.0, "T-band public safety", "identify"),
    (512.0, 608.0, "UHF TV (ATSC)", "decode"),
    (614.0, 698.0, "600 MHz cellular", "view-only"),
    (698.0, 806.0, "700 MHz cellular / FirstNet", "view-only"),
    (806.0, 824.0, "800 MHz public safety (often P25)", "identify"),
    (824.0, 894.0, "850 MHz cellular", "view-only"),
    (902.0, 928.0, "900 MHz ISM (LoRa/pagers/etc.)", "identify"),
    (928.0, 932.0, "paging (POCSAG/FLEX)", "view-only"),
    (935.0, 940.0, "900 MHz SMR/business", "identify"),
    (1090.0, 1090.1, "ADS-B (1090ES)", "decode"),
    (1227.0, 1228.0, "GPS L2", "identify"),
    (1240.0, 1300.0, "23cm ham", "decode"),
    (1350.0, 1400.0, "gov radar / aeronautical", "view-only"),
    (1435.0, 1525.0, "aero telemetry / MSS", "view-only"),
]

POLICY_NOTE = {
    "decode": "openly transmitted - tools may decode",
    "identify": "legal to hear; we label only, no payload tooling",
    "view-only": "PRIVATE/ENCRYPTED - waterfall only, decoding refused",
}


def classify(mhz):
    for lo, hi, name, pol in BANDPLAN:
        if lo <= mhz <= hi:
            return {"band": name, "policy": pol, "range": f"{lo}-{hi} MHz"}
    return {"band": "unallocated / gap", "policy": "identify",
            "range": "-"}


def may_decode(mhz):
    """The guard. Other tools call this BEFORE running any decoder."""
    return classify(mhz)["policy"] == "decode"


def guarded_decode(mhz, decode_fn):
    """Run decode_fn() only if the band policy permits it."""
    c = classify(mhz)
    if c["policy"] != "decode":
        return {"refused": True, "band": c["band"], "policy": c["policy"],
                "reason": POLICY_NOTE[c["policy"]]}
    return {"refused": False, "result": decode_fn()}


def cmd_where(args):
    c = classify(args.mhz)
    print(f"{args.mhz} MHz -> {c['band']}  [{c['range']}]")
    print(f"  policy: {c['policy'].upper()} - {POLICY_NOTE[c['policy']]}")
    print(f"  may_decode() = {may_decode(args.mhz)}")


def cmd_survey(args):
    """Label every carrier in a capture (center freq from --center-mhz)."""
    raw = np.fromfile(args.file, dtype=np.int16).astype(np.float32) / 32768.0
    iq = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
    fs = args.fs
    N = 1 << 15
    seg = iq[:len(iq) // N * N].reshape(-1, N) * np.hanning(N).astype(np.float32)
    P = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
    db = 10 * np.log10(P + 1e-12)
    med = float(np.median(db))
    c = N // 2
    print(f"[survey] {Path(args.file).name} @ {args.center_mhz} MHz center")
    found = 0
    for i in range(2, N - 2):
        if db[i] - med > args.thresh and db[i] >= db[i-1] and db[i] > db[i+1]:
            f = args.center_mhz + (i - c) * fs / N / 1e6
            cl = classify(f)
            gate = "" if cl["policy"] == "decode" else \
                (" [VIEW-ONLY]" if cl["policy"] == "view-only" else " [id-only]")
            print(f"  {f:9.4f} MHz  +{db[i]-med:4.0f} dB  {cl['band']}{gate}")
            found += 1
    if not found:
        print("  no carriers above threshold")


def cmd_selftest(args):
    print("=" * 60)
    print("bandscan self-test (classification + legality guard)")
    print("=" * 60)
    ok = True
    cases = [(90.9, "decode"), (1090.05, "decode"), (162.5, "decode"),
             (7.1, "decode"), (135.0, "decode"), (869.0, "view-only"),
             (751.0, "view-only"), (929.5, "view-only"), (462.0, "identify")]
    for mhz, want in cases:
        got = classify(mhz)["policy"]
        flag = "OK" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {mhz:9.2f} MHz -> {got:<10} (want {want})  {flag}")
    # the guard must REFUSE a decode on a cellular frequency
    r = guarded_decode(869.0, lambda: "SHOULD NOT RUN")
    refused = r.get("refused") is True
    print(f"\n  guard on 869 MHz cellular: refused={refused}  "
          f"{'OK' if refused else 'FAIL - GUARD LEAK'}")
    ok &= refused
    r2 = guarded_decode(90.9, lambda: "ran")
    print(f"  guard on 90.9 FM: ran={r2.get('result')=='ran'}  "
          f"{'OK' if r2.get('result') == 'ran' else 'FAIL'}")
    ok &= r2.get("result") == "ran"
    print("=" * 60)
    print("SELFTEST", "PASS" if ok else "FAIL")
    print("=" * 60)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    w = sub.add_parser("where")
    w.add_argument("mhz", type=float)
    s = sub.add_parser("survey")
    s.add_argument("--file", required=True)
    s.add_argument("--center-mhz", type=float, required=True)
    s.add_argument("--fs", type=float, default=250000)
    s.add_argument("--thresh", type=float, default=15)
    args = ap.parse_args()
    if args.cmd == "selftest":
        sys.exit(cmd_selftest(args))
    elif args.cmd == "where":
        cmd_where(args)
    elif args.cmd == "survey":
        cmd_survey(args)


if __name__ == "__main__":
    main()
