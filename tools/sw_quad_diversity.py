#!/usr/bin/env python3
"""sw_quad_diversity.py - FOUR-branch shortwave diversity: 2 simulcast
outlets x 2 sidebands each, jointly MRC-combined.

Every AM outlet transmits the program twice (mirror sidebands), and a
simulcast broadcaster transmits it on multiple frequencies. A wideband
capture holding two outlets therefore contains FOUR independently-fading
copies of the same audio. Per block: the shared component across branches
is signal by construction (robust median of pairwise cross-covariances);
per-branch noise = var - S; weights = 1/N. Where one sideband takes a
selective notch or one frequency takes a fade, the weights slide to the
clean copies.

Usage:
  python sw_quad_diversity.py CAP.cs16 --fs 250000 --center-khz 4775 \
      --khz-a 4750 --khz-b 4800 [--out-prefix quad]
"""
import argparse, sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import sync_am
from sync_am import CHAN_FS, nail_carrier, write_wav
from scipy.signal import firwin, filtfilt, resample_poly


def channelize(cap, fs, offset_hz):
    from math import gcd
    mm = np.memmap(cap, np.int16, "r")
    raw = mm.astype(np.float32) / 32768.0
    x = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
    n = np.arange(len(x), dtype=np.float64)
    x = x * np.exp(-2j * np.pi * offset_hz / fs * n).astype(np.complex64)
    g = gcd(int(CHAN_FS), int(fs))
    return resample_poly(x, int(CHAN_FS) // g, int(fs) // g).astype(np.complex64)


def sidebands(x):
    """Carrier-lock x, return (U, L, carrier_amp) audio branches."""
    doff = nail_carrier(x)
    n = np.arange(len(x), dtype=np.float64)
    x = x * np.exp(-2j * np.pi * doff / CHAN_FS * n).astype(np.complex64)
    taps_c = firwin(2049, sync_am.CARRIER_BW / (CHAN_FS / 2))
    c = filtfilt(taps_c, [1.0], x)
    amp = np.abs(c).astype(np.float32)
    y = (x * np.conj(c / np.maximum(amp, 1e-12))).astype(np.complex64)
    lp = firwin(513, 2450.0 / (CHAN_FS / 2))
    k = np.arange(len(lp)) - (len(lp) - 1) / 2
    h_u = (lp * np.exp(2j * np.pi * 2550.0 / CHAN_FS * k)).astype(np.complex64)
    h_l = (lp * np.exp(-2j * np.pi * 2550.0 / CHAN_FS * k)).astype(np.complex64)
    U = 2.0 * np.convolve(y, h_u, mode="same").real.astype(np.float32)
    L = 2.0 * np.convolve(y, h_l, mode="same").real.astype(np.float32)
    hp = firwin(4097, 30.0 / (CHAN_FS / 2))
    U = (U - filtfilt(hp, [1.0], U)).astype(np.float32)
    L = (L - filtfilt(hp, [1.0], L)).astype(np.float32)
    return U, L, amp


def align_to(ref, v, span_ms=500):
    """Envelope-align v to ref (audio-rate), sign-fix; returns aligned v."""
    n = min(len(ref), len(v))
    ref, v = ref[:n], v[:n]
    env_r = resample_poly(np.abs(ref), 1000, int(CHAN_FS)).astype(np.float32)
    env_v = resample_poly(np.abs(v), 1000, int(CHAN_FS)).astype(np.float32)
    env_r -= env_r.mean(); env_v -= env_v.mean()
    xc = np.correlate(env_r[span_ms:-span_ms], env_v, mode="valid")
    lag_ms = int(np.argmax(xc)) - span_ms
    lag = int(round(abs(lag_ms) * CHAN_FS / 1000))

    def shift(a, s):
        if s > 0:
            return np.concatenate([np.zeros(s, np.float32), a[:-s]])
        if s < 0:
            return np.concatenate([a[-s:], np.zeros(-s, np.float32)])
        return a
    seg = slice(int(2 * CHAN_FS), int(n - 2 * CHAN_FS))
    if lag:
        cand = [shift(v, lag), shift(v, -lag)]
        dots = [abs(float(np.dot(ref[seg], c[seg]))) for c in cand]
        v = cand[int(np.argmax(dots))]
    if np.dot(ref[seg], v[seg]) < 0:
        v = -v
    return v, lag_ms


def quad_combine(branches, blk_s=0.25):
    """MRC of N aligned same-program branches. S per block = median of
    pairwise cross-covariances; N_i = var_i - S; w_i = 1/N_i."""
    n = min(len(b) for b in branches)
    B = np.stack([b[:n] for b in branches])
    blk = max(1, int(blk_s * CHAN_FS))
    nblk = n // blk
    K = len(branches)
    W = np.empty((K, nblk), np.float32)
    for i in range(nblk):
        seg = B[:, i * blk:(i + 1) * blk]
        segc = seg - seg.mean(axis=1, keepdims=True)
        cov = segc @ segc.T / seg.shape[1]
        pairs = [cov[a, b] for a in range(K) for b in range(a + 1, K)]
        S = max(float(np.median(pairs)), 1e-12)
        for kk in range(K):
            Nk = max(float(cov[kk, kk]) - S, S * 1e-2)
            W[kk, i] = 1.0 / Nk
    Wf = np.stack([np.repeat(np.convolve(W[kk], np.ones(3) / 3, mode="same"), blk)[:nblk * blk]
                   for kk in range(K)])
    Bm = B[:, :nblk * blk]
    out = (Wf * Bm).sum(axis=0) / Wf.sum(axis=0)
    return out.astype(np.float32), W


def pause_floor(a, fs=int(CHAN_FS)):
    from scipy.signal import butter, sosfilt
    sos = butter(4, [300, 3400], btype="band", fs=fs, output="sos")
    v = sosfilt(sos, a).astype(np.float32)
    blk = int(0.05 * fs)
    nb = len(v) // blk
    e = (v[:nb * blk].reshape(nb, blk) ** 2).mean(axis=1)
    return 10 * np.log10(np.percentile(e, 85) / max(np.percentile(e, 15), 1e-15))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--fs", type=float, required=True)
    ap.add_argument("--center-khz", type=float, required=True)
    ap.add_argument("--khz-a", type=float, required=True)
    ap.add_argument("--khz-b", type=float, required=True)
    ap.add_argument("--out-prefix", default="quad")
    a = ap.parse_args()
    lab = Path(__file__).parent.parent / "lab"

    xa = channelize(a.capture, a.fs, (a.khz_a - a.center_khz) * 1000.0)
    xb = channelize(a.capture, a.fs, (a.khz_b - a.center_khz) * 1000.0)
    UA, LA, ampA = sidebands(xa)
    UB, LB, ampB = sidebands(xb)
    print(f"carriers: A {np.median(ampA):.4f}  B {np.median(ampB):.4f}")

    # align outlet B's branches to outlet A (sidebands of one outlet are
    # inherently aligned; cross-outlet path/processing delay is not)
    ref = (UA + LA).astype(np.float32)
    UB2, lag1 = align_to(ref, UB)
    LB2, lag2 = align_to(ref, LB)
    print(f"outlet-B alignment: U {lag1} ms, L {lag2} ms")

    quad, W = quad_combine([UA, LA, UB2, LB2])
    duoA, _ = quad_combine([UA, LA])
    duoB, _ = quad_combine([UB, LB])
    outlets, _ = quad_combine([(UA + LA) / 2, (UB2 + LB2) / 2])

    results = {"A dsb-sync": (UA + LA) / 2, "B dsb-sync": (UB + LB) / 2,
               "A sideband-div": duoA, "B sideband-div": duoB,
               "outlet-div (2br)": outlets, "QUAD (4br)": quad}
    print(f"{'variant':18s} {'pause-floor dB':>14s}")
    for name, aud in results.items():
        print(f"{name:18s} {pause_floor(aud):14.1f}")
        write_wav(str(lab / f"{a.out_prefix}_{name.split()[0]}_{name.split()[1][:4]}.wav"),
                  aud)
    print(f"wavs in {lab}")


if __name__ == "__main__":
    main()
