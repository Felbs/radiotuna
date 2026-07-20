#!/usr/bin/env python3
"""fm_theory_bench.py — is our FM demod on the theory line?

Information theory hands FM a hard curve. Above threshold, for a tone
at f_m with peak deviation D (mod index beta = D/f_m is not the
broadcast figure; for broadcast we use deviation D over audio band W):

    SNR_out = CNR + 10 log10( 3 (D/W)^2 (D/W + 1) ) + G_deemph

with the de-emphasis gain for time constant tau over band W:

    G = 10 log10( (W/f1)^3 / (3 (W/f1 - atan(W/f1))) ),  f1 = 1/(2 pi tau)

For US broadcast (D = 75 kHz, W = 15 kHz, tau = 75 us) the total
processing gain is ~ +39 dB, and the discriminator threshold knee sits
near CNR ~ 10 dB. A demodulator either rides that line or it wastes
signal. This bench: synthesize FM with EXACTLY known CNR in the Carson
bandwidth, run the REAL fm_stereo chain (mono path), measure the output
tone SNR, and print the gap to theory at every CNR.

Verdict semantics:
  gap <= ~1 dB above threshold  -> the demod is provably near-optimal;
                                   remaining quality is propagation, not us
  bigger gap                    -> found engineering headroom, go get it
"""
import numpy as np

import fm_stereo
from fm_stereo import FMStereo, decimate2_cs16, FS_RAW

D = 75_000.0          # peak deviation
W = 15_000.0          # audio bandwidth
TAU = 75e-6
F_TONE = 1_000.0
BW_CARSON = 2 * (D + W)                    # 180 kHz


def theory_gain_db():
    f1 = 1 / (2 * np.pi * TAU)
    x = W / f1
    g_de = 10 * np.log10(x ** 3 / (3 * (x - np.arctan(x))))
    g_fm = 10 * np.log10(3 * (D / W) ** 2 * (D / W + 1))
    return g_fm, g_de


def run_point(cnr_db, secs=6.0, seed=1):
    """Synthesize tone-modulated FM + calibrated noise, demod, measure."""
    fs = FS_RAW
    n = int(secs * fs)
    t = np.arange(n) / fs
    # mono tone at full deviation, plus the 9% pilot so the chain locks
    m = np.sin(2 * np.pi * F_TONE * t)
    comp = 0.9 * m + 0.09 * np.sin(2 * np.pi * 19_000 * t)
    ph = np.cumsum(2 * np.pi * D * comp / fs)
    a_sig = 6000.0
    iq = a_sig * np.exp(1j * ph)
    # noise: complex AWGN with power set so that S/N inside the Carson
    # bandwidth equals cnr_db exactly.  P_sig = a^2. Noise density
    # sigma^2 spread over fs; in-Carson noise = sigma^2 * BW/fs.
    rng = np.random.default_rng(seed)
    p_noise_carson = a_sig ** 2 / (10 ** (cnr_db / 10))
    sigma2 = p_noise_carson * fs / BW_CARSON
    nz = rng.normal(0, np.sqrt(sigma2 / 2), (n, 2)).view(np.complex128).ravel()
    x = iq + nz
    raw = np.empty(2 * n, np.int16)
    raw[0::2] = np.clip(x.real, -32000, 32000).astype(np.int16)
    raw[1::2] = np.clip(x.imag, -32000, 32000).astype(np.int16)

    dem = FMStereo(stereo=False)
    dem.agc_freeze = True
    # full deviation reads ~1.57 rad at the 297.675k composite rate;
    # 12000 keeps peaks at ~19k of the int16's 32k — freezing at the
    # panel's cold-start 60000 CLIPPED the bench and manufactured
    # -18 dB of odd-harmonic THD that we chased for three rounds
    dem.agc = 12_000.0
    outs = []
    for i in range(0, len(raw), 2 * 262144):
        pcm, _ = dem.feed(decimate2_cs16(raw[i:i + 2 * 262144]))
        outs.append(pcm)
    pcm = np.concatenate(outs)
    aud = pcm[0::2].astype(np.float64)     # mono: L == R
    aud = aud[int(1.0 * fm_stereo.FS_AUDIO):]          # settle
    # tone power via coherent bin. The residual must count ONLY noise:
    # the pilot leaks through the 15 kHz audio FIR and the tone carries
    # harmonics — both deterministic, phase-stable lines that pinned an
    # earlier version of this measurement at 10.4 dB forever. Subtract
    # every known line coherently, THEN call the remainder noise.
    k = np.arange(len(aud))
    # the TRUE sample rate (FSC/6 = 49612.5), not the WAV header's
    # rounded int: 10 ppm of reference error leaves a -21 dB residue
    # of an imperfectly-subtracted fundamental that reads as a noise
    # floor. The header lies; the samples don't.
    fs_a = fm_stereo.FSC / 6

    def coherent(f):
        ref = np.exp(-2j * np.pi * f * k / fs_a)
        c = (aud * ref).mean()
        return c, ref

    c1, _ = coherent(F_TONE)
    p_tone = 2 * np.abs(c1) ** 2
    resid = aud.astype(np.complex128)
    for f in (F_TONE, 2 * F_TONE, 3 * F_TONE, 4 * F_TONE, 5 * F_TONE,
              19_000.0, 19_000.0 - F_TONE, 19_000.0 + F_TONE):
        c, ref = coherent(f)
        resid = resid - 2 * np.real(c * np.conj(ref))
    p_noise = max(float(np.real(resid * np.conj(resid)).mean()), 1e-12)
    return 10 * np.log10(p_tone / p_noise)


def main():
    g_fm, g_de = theory_gain_db()
    print(f"theory: FM gain {g_fm:.1f} dB + de-emphasis {g_de:.1f} dB "
          f"= {g_fm + g_de:.1f} dB above CNR (above threshold)")
    print(f"{'CNR':>5} {'theory':>7} {'ours':>7} {'gap':>6}")
    pts = []
    for cnr in (30, 25, 20, 16, 13, 11, 9, 7, 5):
        th = cnr + g_fm + g_de
        ours = run_point(cnr)
        pts.append((cnr, th, ours))
        print(f"{cnr:5.0f} {th:7.1f} {ours:7.1f} {ours - th:+6.1f}")
    above = [o - t for c, t, o in pts if c >= 13]
    mean_gap, std_gap = float(np.mean(above)), float(np.std(above))
    # the broadcast standard's own overhead vs the ideal-FM formula:
    # 10% of deviation is reserved for the pilot, so program audio
    # gets D' = 0.9 D -> a fixed, unavoidable offset
    Dp = 0.9 * D
    tax = 10 * np.log10(((Dp / W) ** 2 * (Dp / W + 1))
                        / ((D / W) ** 2 * (D / W + 1)))
    print(f"\nabove threshold (CNR >= 13): gap {mean_gap:+.1f} dB, "
          f"std {std_gap:.2f} dB")
    print(f"of which the pilot/deviation budget costs {tax:+.1f} dB "
          f"by standard — implementation loss "
          f"{mean_gap - tax:+.1f} dB")
    if std_gap < 0.4 and (mean_gap - tax) > -1.5:
        print("VERDICT: the demod RIDES THE THEORY LINE (constant "
              "offset = accounted overhead, <1.5 dB implementation "
              "loss). Above threshold there is nothing meaningful "
              "left to win in the demodulator; remaining wins live "
              "at the threshold knee (PLL extension ~2-3 dB) and in "
              "antenna/propagation.")
    else:
        print("VERDICT: measurable gap to theory — engineering "
              "headroom exists in the demod. Go find it.")


if __name__ == "__main__":
    main()
