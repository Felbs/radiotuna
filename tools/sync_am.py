"""sync_am.py - Radio Tuna campaign: synchronous AM detection (the IMPROVE claim).

Envelope detection (every $10 radio) computes |carrier + audio| - it needs
the carrier to be the biggest thing in the channel. Skywave at night breaks
that promise: selective fading notches the carrier itself, the envelope
folds over, and you hear the classic distorted "underwater" AM.

Synchronous detection regenerates the carrier PHASE (a narrowband PLL /
carrier filter), multiplies it back in, and takes the real part. The
carrier's AMPLITUDE drops out entirely - a faded carrier still tells us
its phase, so the audio survives the notch.

This tool proves the improvement two ways:
  selftest - synthesize AM, notch the carrier -20 dB (worst-case selective
             fade), decode both ways vs the KNOWN message: output SNR in dB.
  decode   - run both detectors on a real capture (cs16), report carrier
             fade statistics + detector divergence during fades, and write
             env.wav / sync.wav so ears can judge (the DATAMOSH law: the
             presentation layer is a measurement instrument).

Example:  python sync_am.py decode --file cap.cs16 --khz 1110 --center-khz 1150
"""
import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly, firwin, filtfilt

HERE = Path(__file__).resolve().parent
LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)

CHAN_FS = 20000.0        # channelized complex rate (+/-10 kHz AM channel)
AUDIO_LP = 5000.0        # audio lowpass
CARRIER_BW = 30.0        # carrier-tracking filter bandwidth (Hz)


def channelize(iq, fs, off_hz):
    """Mix the target carrier to ~DC and decimate to CHAN_FS."""
    n = np.arange(len(iq), dtype=np.float64)
    x = iq * np.exp(-2j * np.pi * off_hz / fs * n).astype(np.complex64)
    from math import gcd
    g = gcd(int(CHAN_FS), int(fs))
    return resample_poly(x, int(CHAN_FS) // g, int(fs) // g).astype(np.complex64)


def nail_carrier(x):
    """Find the exact residual carrier offset (Hz) by FFT peak near DC."""
    n = 1 << 15
    seg = x[:len(x) // n * n].reshape(-1, n) * np.hanning(n).astype(np.float32)
    P = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / CHAN_FS))
    m = np.abs(f) < 500.0
    return f[m][np.argmax(P[m])]


def detect_both(x):
    """Return (audio_env, audio_sync, carrier_amp) at CHAN_FS."""
    # re-center on the exact carrier so the narrow filter holds it
    doff = nail_carrier(x)
    n = np.arange(len(x), dtype=np.float64)
    x = x * np.exp(-2j * np.pi * doff / CHAN_FS * n).astype(np.complex64)

    # carrier estimate: narrow complex lowpass = filter-based PLL
    taps_c = firwin(2049, CARRIER_BW / (CHAN_FS / 2))
    c = filtfilt(taps_c, [1.0], x)
    amp = np.abs(c).astype(np.float32)
    phase = c / np.maximum(amp, 1e-12)

    audio_lp = firwin(257, AUDIO_LP / (CHAN_FS / 2))
    env = np.abs(x).astype(np.float32)
    sync = (x * np.conj(phase)).real.astype(np.float32)
    out = []
    for a in (env, sync):
        a = filtfilt(audio_lp, [1.0], a)
        a = a - filtfilt(firwin(4097, 30.0 / (CHAN_FS / 2)), [1.0], a)  # DC/rumble block
        out.append(a.astype(np.float32))
    return out[0], out[1], amp


def detect_diversity(x, blk_s=0.25):
    """Sideband-diversity synchronous detection: AM transmits the audio
    TWICE (mirror sidebands). Demodulate each sideband separately against
    the recovered carrier phase, estimate per-block signal/noise from the
    cross-covariance (the shared component is signal by construction),
    and MRC-combine. Where one sideband carries splatter or a selective
    notch, the weights slide to the clean one - an equalizer whose
    training signal is the broadcast's own redundancy.

    Returns (audio_div, audio_sync, diag) at CHAN_FS."""
    doff = nail_carrier(x)
    n = np.arange(len(x), dtype=np.float64)
    x = x * np.exp(-2j * np.pi * doff / CHAN_FS * n).astype(np.complex64)
    taps_c = firwin(2049, CARRIER_BW / (CHAN_FS / 2))
    c = filtfilt(taps_c, [1.0], x)
    amp = np.abs(c).astype(np.float32)
    y = (x * np.conj(c / np.maximum(amp, 1e-12))).astype(np.complex64)

    # one-sided complex bandpasses: 100-5000 Hz above / below the carrier
    lp = firwin(513, 2450.0 / (CHAN_FS / 2))
    # modulate about the CENTER tap - modulating from k=0 leaves a constant
    # passband phase rotation that mixes audio into its Hilbert transform
    k = np.arange(len(lp)) - (len(lp) - 1) / 2
    h_u = (lp * np.exp(2j * np.pi * 2550.0 / CHAN_FS * k)).astype(np.complex64)
    h_l = (lp * np.exp(-2j * np.pi * 2550.0 / CHAN_FS * k)).astype(np.complex64)
    U = 2.0 * np.convolve(y, h_u, mode="same").real.astype(np.float32)
    L = 2.0 * np.convolve(y, h_l, mode="same").real.astype(np.float32)

    blk = max(1, int(blk_s * CHAN_FS))
    nblk = len(U) // blk
    wU = np.empty(nblk, np.float32)
    wL = np.empty(nblk, np.float32)
    gain_db = np.empty(nblk, np.float32)
    for i in range(nblk):
        u = U[i * blk:(i + 1) * blk]
        l = L[i * blk:(i + 1) * blk]
        S = max(float(np.mean(u * l)), 1e-12)
        NU = max(float(np.var(u)) - S, S * 1e-2)
        NL = max(float(np.var(l)) - S, S * 1e-2)
        wU[i], wL[i] = 1.0 / NU, 1.0 / NL
        snr_mrc = S / NU + S / NL
        snr_dsb = 4.0 * S / (NU + NL)
        gain_db[i] = 10 * np.log10(snr_mrc / snr_dsb)
    # smooth weights to avoid clicks, expand to sample rate
    def expand(w):
        w = np.convolve(w, np.ones(3, np.float32) / 3, mode="same")
        return np.repeat(w, blk)[:nblk * blk]
    eU, eL = expand(wU), expand(wL)
    div = (eU * U[:nblk * blk] + eL * L[:nblk * blk]) / (eU + eL)
    sync = 0.5 * (U + L)                     # plain DSB coherent, same path

    audio_lp = firwin(257, AUDIO_LP / (CHAN_FS / 2))
    hp = firwin(4097, 30.0 / (CHAN_FS / 2))
    out = []
    for a in (div, sync[:len(div)]):
        a = filtfilt(audio_lp, [1.0], a)
        a = a - filtfilt(hp, [1.0], a)
        out.append(a.astype(np.float32))
    diag = {"mean_gain_db": round(float(np.mean(gain_db)), 2),
            "p95_gain_db": round(float(np.percentile(gain_db, 95)), 2),
            "tilt_db": round(float(10 * np.log10(np.mean(1 / wU) / np.mean(1 / wL))), 1)}
    return out[0], out[1], diag


def write_wav(path, audio, fs=int(CHAN_FS)):
    a = audio / max(np.percentile(np.abs(audio), 99.9), 1e-9)
    a = np.clip(a, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes((a * 32000).astype(np.int16).tobytes())


def cmd_selftest(args):
    print("=" * 60)
    print("sync_am self-test: carrier notched -20 dB (selective fade)")
    print("=" * 60)
    rng = np.random.default_rng(7)
    fs = CHAN_FS
    t = np.arange(int(20 * fs)) / fs
    # program audio: three tones + band noise, 80% modulation
    m = (0.4 * np.sin(2 * np.pi * 440 * t) + 0.25 * np.sin(2 * np.pi * 1100 * t)
         + 0.15 * np.sin(2 * np.pi * 2500 * t))
    s = (1.0 + m).astype(np.complex64)                 # AM at complex baseband
    # selective fade: carrier line attenuated to -20 dB for the middle 10 s
    g = np.zeros(len(t), np.float32)
    mid = (t > 5) & (t < 15)
    g[mid] = 0.9
    y = s - g * 1.0                                    # subtract 90% of the carrier
    y += (rng.normal(0, 0.01, len(y)) + 1j * rng.normal(0, 0.01, len(y))).astype(np.complex64)

    env, sync, amp = detect_both(y)
    ref_lp = firwin(257, AUDIO_LP / (fs / 2))
    ref = filtfilt(ref_lp, [1.0], m).astype(np.float32)

    def out_snr(a, sel):
        aa, rr = a[sel], ref[sel]
        gain = np.dot(aa, rr) / np.dot(rr, rr)
        err = aa - gain * rr
        return 10 * np.log10(np.dot(rr, rr) * gain ** 2 / max(np.dot(err, err), 1e-12))

    k = int(1 * fs)
    clean = np.zeros(len(t), bool); clean[k:int(4 * fs)] = True
    faded = np.zeros(len(t), bool); faded[int(6 * fs):int(14 * fs)] = True
    se_c, ss_c = out_snr(env, clean), out_snr(sync, clean)
    se_f, ss_f = out_snr(env, faded), out_snr(sync, faded)
    print(f"  clean carrier : envelope {se_c:5.1f} dB   sync {ss_c:5.1f} dB")
    print(f"  carrier -20dB : envelope {se_f:5.1f} dB   sync {ss_f:5.1f} dB"
          f"   -> sync wins by {ss_f - se_f:+.1f} dB")

    print("-" * 60)
    print("sideband-diversity: splatter jamming the UPPER sideband only")
    y2 = s + (rng.normal(0, 0.01, len(t)) + 1j * rng.normal(0, 0.01, len(t))).astype(np.complex64)
    # interferer: noise burst occupying +1..+4 kHz (upper sideband only)
    ni = (rng.normal(0, 1.0, len(t)) + 1j * rng.normal(0, 1.0, len(t))).astype(np.complex64)
    lp_i = firwin(513, 1500.0 / (fs / 2))
    ni = filtfilt(lp_i, [1.0], ni)
    ni = ni * np.exp(2j * np.pi * 2500.0 / fs * np.arange(len(t)))
    y2 = y2 + 0.35 * ni.astype(np.complex64)
    div2, sync2, diag2 = detect_diversity(y2)
    sel = np.zeros(len(div2), bool); sel[int(2 * fs):int(18 * fs)] = True
    sd, ssy = out_snr(div2, sel), out_snr(sync2[:len(div2)], sel)
    print(f"  USB jammed    : sync(DSB) {ssy:5.1f} dB   diversity {sd:5.1f} dB"
          f"   -> diversity wins by {sd - ssy:+.1f} dB  (tilt {diag2['tilt_db']} dB)")
    # clean symmetric case: diversity must not hurt
    y3 = s + (rng.normal(0, 0.05, len(t)) + 1j * rng.normal(0, 0.05, len(t))).astype(np.complex64)
    div3, sync3, diag3 = detect_diversity(y3)
    sd3, ssy3 = out_snr(div3, sel), out_snr(sync3[:len(div3)], sel)
    print(f"  clean symmetric: sync(DSB) {ssy3:5.1f} dB   diversity {sd3:5.1f} dB"
          f"   (must be ~equal; gain dial {diag3['mean_gain_db']} dB)")

    ok = ((ss_f - se_f) > 6.0 and ss_c > 20.0 and abs(se_c - ss_c) < 3.0
          and (sd - ssy) > 6.0 and abs(sd3 - ssy3) < 1.5)
    print("=" * 60)
    print("SELFTEST", "PASS" if ok else "FAIL")
    print("=" * 60)
    return 0 if ok else 1


def cmd_decode(args):
    raw = np.fromfile(args.file, dtype=np.int16).astype(np.float32) / 32768.0
    iq = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
    fs = args.fs
    side = Path(args.file + ".json")
    if side.exists():
        meta = json.loads(side.read_text())
        fs = float(meta.get("fs_hz", fs))
        if args.center_khz is None and "freq_hz" in meta:
            args.center_khz = meta["freq_hz"] / 1e3
    off = (args.khz - args.center_khz) * 1e3
    print(f"[sync_am] {args.khz:.0f} kHz  (offset {off:+.0f} Hz in capture, fs {fs:.0f})")
    x = channelize(iq, fs, off)
    env, sync, amp = detect_both(x)

    med = np.median(amp)
    fade_db = 20 * np.log10(np.maximum(amp, 1e-12) / max(med, 1e-12))
    frac10 = float((fade_db < -10).mean())
    frac6 = float((fade_db < -6).mean())
    print(f"[carrier] median amp {med:.4f}   fades: {100*frac6:.1f}% below -6 dB, "
          f"{100*frac10:.1f}% below -10 dB   deepest {fade_db.min():.1f} dB")

    # divergence: where the two detectors disagree, envelope is the liar
    # (they agree exactly when the carrier dominates)
    g = np.dot(env, sync) / max(np.dot(sync, sync), 1e-12)
    d = env - g * sync
    strong = fade_db > -3
    faded = fade_db < -8
    p_ref = np.mean(sync[strong] ** 2) if strong.any() else 1e-12
    div_strong = 10 * np.log10(np.mean(d[strong] ** 2) / p_ref + 1e-12) if strong.any() else float("nan")
    div_faded = 10 * np.log10(np.mean(d[faded] ** 2) / p_ref + 1e-12) if faded.any() else float("nan")
    print(f"[diverge] env-vs-sync residual: strong-carrier {div_strong:6.1f} dB"
          f"   faded {div_faded:6.1f} dB  (rel. program power)")
    if faded.any() and div_faded - div_strong > 6:
        print(f"[verdict] envelope detector distorts by {div_faded - div_strong:.1f} dB "
              f"extra during fades on this station - sync audio is the keeper")
    elif not faded.any():
        print("[verdict] carrier never faded >8 dB in this capture - detectors equivalent here")

    div, _sync_dsb, diag = detect_diversity(x)
    print(f"[divers ] MRC gain over plain sync: mean {diag['mean_gain_db']:+.2f} dB, "
          f"p95 {diag['p95_gain_db']:+.2f} dB   noise tilt U/L {diag['tilt_db']:+.1f} dB")

    tag = f"{int(args.khz)}"
    for name, a in (("env", env), ("sync", sync), ("div", div)):
        p = LAB / f"am{tag}_{name}.wav"
        write_wav(p, a)
        print(f"[wav] {p}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    d = sub.add_parser("decode")
    d.add_argument("--file", required=True)
    d.add_argument("--khz", type=float, required=True, help="station frequency, kHz")
    d.add_argument("--center-khz", type=float, default=None,
                   help="capture center, kHz (default: from .json sidecar)")
    d.add_argument("--fs", type=float, default=250000)
    args = ap.parse_args()
    if args.cmd == "selftest":
        sys.exit(cmd_selftest(args))
    sys.exit(cmd_decode(args))


if __name__ == "__main__":
    main()
