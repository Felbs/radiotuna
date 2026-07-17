"""drm.py - Radio Tuna campaign: DRM digital shortwave, stage 1 (acquisition).

DRM30 is digital radio hiding on the shortwave bands: COFDM + QAM with
deep FEC - clean audio where analog hisses. Stage 1 is honest acquisition:
find a DRM transmission and identify its OFDM robustness mode from the
guard-interval fingerprint. Stage 2 (sync + QAM + FEC + xHE-AAC audio)
builds on a confirmed live acquisition.

The detector is carrier-robust BY CONSTRUCTION, because v0 got fooled:
a plain AM carrier self-correlates at EVERY lag and lit up all four
modes equally (rho 0.61 across the board, 7/17 23:40Z). The fix, kept
as law: (1) normalize each mode's GI correlation against CONTROL lags
that match no mode, and (2) gate on spectral occupancy - real COFDM
fills its ~10 kHz block nearly flat; a carrier occupies a few bins.

Modes:
  selftest - synthetic DRM mode-B burst must lock; a pure carrier and
             plain noise must NOT (the tone-trap regression test)
  acquire  - analyze a capture file (cs16) or take a live capture at
             --khz, report mode verdict + numbers

DRM in North America: WINB 9265 kHz runs DRM slots; European/African
outlets reachable at night. Pair with hf_knob's clock to learn when.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hf_knob import open_sdr, grab, FS, _ensure_sdr_dll_path   # noqa: E402

_ensure_sdr_dll_path()
LAB = HERE.parent / "lab"

FS12 = 12_000
MODES = {"A": (288, 32), "B": (256, 64), "C": (176, 64), "D": (112, 88)}
CONTROL_LAGS = (97, 150, 233, 301)      # match no DRM Nu


def to_12k(iq, fs):
    from scipy.signal import resample_poly
    from math import gcd
    g = gcd(FS12, int(fs))
    return resample_poly(iq, FS12 // g, int(fs) // g).astype(np.complex64)


def _gi_rho(x, Nu, Ng, fold_Ns=None):
    pwr = float(np.mean(np.abs(x) ** 2)) + 1e-12
    prod = x[:-Nu] * np.conj(x[Nu:])
    c = np.convolve(prod, np.ones(Ng, np.float32) / Ng, mode="valid")
    if fold_Ns:
        nsym = len(c) // fold_Ns
        if nsym < 4:
            return 0.0
        return float(np.abs(c[:nsym * fold_Ns].reshape(nsym, fold_Ns)
                            .mean(axis=0)).max() / pwr)
    return float(np.mean(np.abs(c)) / pwr)


def occupancy(x, bw_hz=10_000, thresh_db=6.0):
    N = 4096
    if len(x) < 4 * N:
        return 0.0
    seg = x[:len(x) // N * N].reshape(-1, N) * np.hanning(N).astype(np.float32)
    P = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
    db = 10 * np.log10(P + 1e-12)
    floor = float(np.percentile(db, 10))   # NOT the median: a wide OFDM
    k = int((bw_hz / 2) / (FS12 / N))      # block IS the median of its band
    band = db[N // 2 - k:N // 2 + k] - floor
    return float(np.sum(band > thresh_db) / len(band))


def acquire(x):
    """x at 12 kHz. Returns the verdict dict."""
    occ = occupancy(x)
    ctrl = max(_gi_rho(x, lag, 64) for lag in CONTROL_LAGS)
    scores = {}
    for m, (Nu, Ng) in MODES.items():
        rho = _gi_rho(x, Nu, Ng, fold_Ns=Nu + Ng)
        scores[m] = round(rho / (ctrl + 0.05), 2)   # normalized vs tone floor
    best = max(scores, key=scores.get)
    locked = scores[best] > 2.0 and occ > 0.5
    return {"locked": bool(locked), "mode": best if locked else None,
            "scores": scores, "control_rho": round(ctrl, 3),
            "occupancy": round(occ, 2)}


# ==========================================================================
def synth_ofdm(mode="B", nsym=120, fs=FS12, seed=6, carrier=False,
               noise=0.05):
    rng = np.random.default_rng(seed)
    if carrier:                       # the tone trap that fooled v0
        n = np.arange(int(nsym * 320))
        x = 0.5 * np.exp(2j * np.pi * 9.0 / fs * n)
        return (x + rng.normal(0, noise, len(x))
                + 1j * rng.normal(0, noise, len(x))).astype(np.complex64)
    Nu, Ng = MODES[mode]
    used = int(Nu * 0.8)              # fill ~10 kHz of the 12 kHz grid, like real DRM
    out = []
    for _ in range(nsym):
        X = np.zeros(Nu, np.complex64)
        idx = np.r_[1:used // 2, Nu - used // 2:Nu]
        X[idx] = (rng.choice([-1, 1], len(idx))
                  + 1j * rng.choice([-1, 1], len(idx))) / np.sqrt(2)
        s = np.fft.ifft(X) * np.sqrt(Nu)
        out.append(np.r_[s[-Ng:], s])           # cyclic prefix
    x = np.concatenate(out).astype(np.complex64) * 0.3
    x += (rng.normal(0, noise, len(x)) + 1j * rng.normal(0, noise, len(x))
          ).astype(np.complex64)
    return x


def cmd_selftest(args):
    print("=" * 62)
    print("DRM acquisition self-test (incl. the tone-trap regression)")
    print("=" * 62)
    ok = True
    v = acquire(synth_ofdm("B"))
    print(f"  synthetic mode-B : locked={v['locked']} mode={v['mode']} "
          f"scores={v['scores']} occ={v['occupancy']}")
    ok &= v["locked"] and v["mode"] == "B"
    v = acquire(synth_ofdm(carrier=True))
    print(f"  pure carrier trap: locked={v['locked']} (must be False) "
          f"ctrl_rho={v['control_rho']} occ={v['occupancy']}")
    ok &= not v["locked"]
    rng = np.random.default_rng(7)
    noise = (rng.normal(0, 1, 60000) + 1j * rng.normal(0, 1, 60000)
             ).astype(np.complex64)
    v = acquire(noise)
    print(f"  pure noise       : locked={v['locked']} (must be False)")
    ok &= not v["locked"]
    print("=" * 62)
    print("SELFTEST", "PASS" if ok else "FAIL")
    print("=" * 62)
    return 0 if ok else 1


def cmd_acquire(args):
    if args.file:
        raw = np.fromfile(args.file, dtype=np.int16).astype(np.float32) / 32768.0
        iq = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
        fs = args.fs
        print(f"[acquire] file {Path(args.file).name}: {len(iq)/fs:.1f}s")
    else:
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX
        sdr, st = open_sdr(args.antenna)
        sdr.setFrequency(SOAPY_SDR_RX, 0, args.khz * 1e3)
        time.sleep(0.2)
        iq = grab(sdr, st, args.secs)
        sdr.deactivateStream(st)
        sdr.closeStream(st)
        fs = FS
        print(f"[acquire] {args.khz} kHz live: {len(iq)/fs:.1f}s captured")
    x = to_12k(iq, fs)
    v = acquire(x)
    print(f"[verdict] locked={v['locked']}  mode={v['mode']}")
    print(f"          scores={v['scores']}  (need >2.0 + occupancy>0.5)")
    print(f"          occupancy={v['occupancy']}  control_rho={v['control_rho']}")
    if v["locked"]:
        print("  -> DRM TRANSMISSION CONFIRMED - stage 2 (sync/QAM/FEC) has a target!")
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    a = sub.add_parser("acquire")
    a.add_argument("--khz", type=float, default=9265)
    a.add_argument("--secs", type=float, default=30)
    a.add_argument("--antenna", default="Antenna C")
    a.add_argument("--file")
    a.add_argument("--fs", type=float, default=250000)
    args = ap.parse_args()
    if args.cmd == "selftest":
        sys.exit(cmd_selftest(args))
    elif args.cmd == "acquire":
        cmd_acquire(args)


if __name__ == "__main__":
    main()
