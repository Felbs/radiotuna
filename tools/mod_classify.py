"""mod_classify.py - Radio Tuna: per-carrier modulation classifier.

The RTL-ML borrow, done the Radio Tuna way: where they train a Random
Forest on spectrograms, we start with PHYSICS - every feature below is
a measured property with a meaning, so every verdict is explainable.
(A trained model can replace the rules later; the features stay.)

Classes:
  AM-VOICE   symmetric sidebands, breathing envelope, voice-shaped audio
  AM-MUSIC   like voice but denser envelope (less pause structure)
  CW/KEYED   on-off keyed carrier (beacons, morse)
  DATA       constant-envelope / flat-topped digital (DRM, RTTY, FSK -
             the 6170 trap: big signal, nothing to listen to)
  CARRIER    unmodulated carrier (open transmitter, birdie)
  NOISE      no coherent signal
  JAMMER?    strong, wideband, noise-like modulation

Input: complex channel capture (carrier near DC) at fs >= 12.5 kHz,
2 s or more.  python mod_classify.py selftest  proves the rules.
"""
import sys
from pathlib import Path

import numpy as np
from scipy.signal import firwin, filtfilt, resample_poly

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import am_best


def features(x, fs):
    """Measured physics of one channel capture."""
    f = {}
    doff, csnr, ncoch, dom = am_best.nail_carrier(x, fs)
    f["carrier_db"] = round(float(csnr), 1)
    n = np.arange(len(x), dtype=np.float64)
    x = (x * np.exp(-2j * np.pi * doff / fs * n)).astype(np.complex64)

    # occupied bandwidth + spectral top-flatness (data modes fill their
    # channel like a brick; analog rolls off)
    m = 1 << 14
    seg = x[:len(x) // m * m].reshape(-1, m) * np.hanning(m).astype(np.float32)
    P = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
    fr = np.fft.fftshift(np.fft.fftfreq(m, 1 / fs))
    Pdb = 10 * np.log10(P + 1e-18)
    floor = np.percentile(Pdb, 20)
    pk = Pdb.max()
    # bandwidth of the DOMINANT feature: contiguous span around the peak
    # (counting every bin above a floor threshold measures the noise
    # floor's texture on real captures, not the signal - the NDB lesson)
    ipk = int(np.argmax(Pdb))
    lvl = max(pk - 15.0, floor + 6.0)
    lo_i = ipk
    while lo_i > 0 and Pdb[max(0, lo_i - 8):lo_i].max() > lvl:
        lo_i -= 8
    hi_i = ipk
    while hi_i < len(Pdb) - 9 and Pdb[hi_i:hi_i + 8].max() > lvl:
        hi_i += 8
    f["bw_hz"] = int((hi_i - lo_i) * fs / m)
    # second spectral feature of comparable power (FSK mirror tone):
    # a tone PAIR must never read as on-off keying
    Ptmp = Pdb.copy()
    Ptmp[max(0, lo_i - 20):hi_i + 20] = floor
    i2 = int(np.argmax(Ptmp))
    f["tone_pair"] = int(Ptmp[i2] > pk - 8.0 and
                         abs(fr[i2] - fr[ipk]) > 1000.0)
    dom_lo, dom_hi = fr[lo_i], fr[hi_i]
    occ = np.zeros(len(Pdb), bool)
    occ[lo_i:hi_i + 1] = True
    inb = Pdb[occ] if occ.any() else Pdb
    f["flat_top_db"] = round(float(np.percentile(inb, 90) - np.percentile(inb, 40)), 1)
    # sideband symmetry (DSB AM mirrors; FSK/SSB does not)
    pos = P[(fr > 150) & (fr < 5000)]
    neg = P[(fr < -150) & (fr > -5000)][::-1]
    k = min(len(pos), len(neg))
    if k > 10:
        lp, ln = np.log10(pos[:k] + 1e-18), np.log10(neg[:k] + 1e-18)
        f["symmetry"] = round(float(np.corrcoef(lp, ln)[0, 1]), 2)
    else:
        f["symmetry"] = 0.0

    # SIDEBAND COHERENCE - the true analog-AM fingerprint: DSB AM puts
    # the SAME audio in both sidebands (what the MRC exploits); FSK/data
    # alternates energy between them. corr(U,L) says which world we're in.
    y = x
    U, L = am_best._sidebands(y, fs)
    k = min(len(U), len(L))
    if k > 1000:
        f["coherence"] = round(float(np.corrcoef(U[:k], L[:k])[0, 1]), 2)
    else:
        f["coherence"] = 0.0

    # TWO envelopes, two questions. env_cv (constancy, for DATA) comes
    # from the wideband magnitude - abs FIRST, then decimate (lowpassing
    # complex FSK first fakes on-off keying). bimodal (keying, for CW)
    # comes from the dominant feature's OWN band, so a weak narrow
    # beacon isn't diluted by out-of-band noise.
    env = resample_poly(np.abs(x), 1, max(1, int(fs // 1000)))
    env = env / max(float(np.median(env)), 1e-9)
    f["env_cv"] = round(float(np.std(env)), 3)
    fc_dom = (dom_lo + dom_hi) / 2
    bw_dom = max(120.0, (dom_hi - dom_lo) * 1.2)
    nn2 = np.arange(len(x), dtype=np.float64)
    xn = (x * np.exp(-2j * np.pi * fc_dom / fs * nn2)).astype(np.complex64)
    xn = filtfilt(firwin(513, min(0.45, bw_dom / fs)), [1.0], xn)
    env = resample_poly(np.abs(xn), 1, max(1, int(fs // 1000)))
    env = env / max(float(np.median(env)), 1e-9)
    # bimodality: keyed carriers live at two levels
    hi, lo = np.percentile(env, 85), np.percentile(env, 15)
    mid = (hi + lo) / 2
    near_mid = float(((env > mid * 0.9) & (env < mid * 1.1)).mean())
    f["bimodal"] = round(1.0 - near_mid, 2) if hi > 1.5 * max(lo, 1e-6) else 0.0
    # mostly-on keying (an NDB idles ON and gaps only for its ID): the
    # percentiles both sit at "on", but the OFF dips are unmistakable
    f["dips"] = round(float((env < 0.55).mean()), 3)

    # demodulated-audio character (voice breathes and slopes; data hisses)
    if len(x) >= 4 * fs // 2:
        a, d = am_best.best_chunk(x.copy(), fs, rescue=False)
        aa = a[int(0.2 * fs):-int(0.2 * fs)] if len(a) > fs else a
        seg2 = 1 << 12
        S = np.abs(np.fft.rfft(aa[:len(aa) // seg2 * seg2].reshape(-1, seg2),
                               axis=1)) ** 2
        Sm = S.mean(axis=0) + 1e-18
        fa = np.fft.rfftfreq(seg2, 1 / fs)
        band = (fa > 200) & (fa < 4000)
        g = np.exp(np.mean(np.log(Sm[band])))
        f["aud_flatness"] = round(float(g / np.mean(Sm[band])), 3)
        # temporal breathing: block-rms spread of the audio
        blk = aa[:len(aa) // 2048 * 2048].reshape(-1, 2048)
        rms = np.sqrt((blk ** 2).mean(axis=1))
        f["aud_breathe"] = round(float(np.percentile(rms, 90) /
                                       max(np.percentile(rms, 20), 1e-9)), 2)
        f["mod_depth"] = d.get("audio_snr_db", 0)
    else:
        f["aud_flatness"], f["aud_breathe"], f["mod_depth"] = 1.0, 1.0, 0
    return f


def classify(x, fs):
    """(label, confidence, features) - physics rules, each explainable."""
    f = features(x, fs)
    c = f["carrier_db"]
    if c < 10 and f["bw_hz"] < 500:
        return "NOISE", 0.9, f
    keyed = f["bimodal"] > 0.55 or 0.01 < f["dips"] < 0.6
    if keyed and f["bw_hz"] < 1500 and not f["tone_pair"]:
        return "CW/KEYED", 0.85, f
    if c >= 20 and f["env_cv"] < 0.04 and f["bw_hz"] < 800:
        return "CARRIER", 0.85, f
    if f["coherence"] >= 0.5:            # both sidebands carry ONE audio
        label = "AM-VOICE" if f["aud_breathe"] > 2.2 else "AM-MUSIC"
        return label, 0.85, f
    if f["bw_hz"] > 6000 and f["aud_flatness"] > 0.5 and c < 16:
        return "JAMMER?", 0.6, f
    if f["bw_hz"] > 3000:
        return "DATA", (0.8 if f["flat_top_db"] < 8 else 0.6), f
    if c >= 12:
        return "DATA", 0.5, f            # coherent but not analog-shaped
    return "NOISE", 0.5, f


# ---------------------------------------------------------------------
def _mk(fs, secs, kind, rng):
    t = np.arange(int(secs * fs)) / fs
    n = (rng.normal(0, .02, len(t)) + 1j * rng.normal(0, .02, len(t)))
    if kind == "am_voice":
        # syllable-gated bandlimited program
        m = filtfilt(firwin(257, 3000 / (fs / 2)), [1.0],
                     rng.normal(0, 1, len(t)))
        gate = (np.sin(2 * np.pi * 2.7 * t) > -0.2).astype(float)
        gate = filtfilt(firwin(101, 8 / (fs / 2)), [1.0], gate)
        return (1 + 0.7 * np.clip(m * gate / np.std(m), -1, 1)) + n
    if kind == "am_music":
        m = filtfilt(firwin(257, 4500 / (fs / 2)), [1.0],
                     rng.normal(0, 1, len(t)))
        return (1 + 0.6 * np.clip(m / np.std(m) * 0.5, -1, 1)) + n
    if kind == "cw":
        key = (np.sin(2 * np.pi * 0.9 * t) > 0).astype(float)
        key = filtfilt(firwin(201, 30 / (fs / 2)), [1.0], key)
        return key * np.exp(2j * np.pi * 30 * t) + n
    if kind == "carrier":
        return np.exp(2j * np.pi * 12 * t) + n
    if kind == "data":
        sym = rng.choice([-1, 1], int(secs * 2400) + 1)
        up = np.repeat(sym, int(np.ceil(len(t) / len(sym))))[:len(t)]
        ph = np.cumsum(2 * np.pi * (2400 * up) / fs)
        return np.exp(1j * ph) + n            # wideband FSK brick
    return rng.normal(0, .3, len(t)) + 1j * rng.normal(0, .3, len(t))


def selftest():
    fs = 20_000.0
    rng = np.random.default_rng(11)
    want = {"am_voice": "AM-VOICE", "am_music": "AM-MUSIC", "cw": "CW/KEYED",
            "carrier": "CARRIER", "data": "DATA", "noise": "NOISE"}
    ok = True
    print("=" * 56)
    for kind, expect in want.items():
        x = _mk(fs, 6, kind, rng).astype(np.complex64)
        label, conf, f = classify(x, fs)
        hit = label == expect or (expect.startswith("AM") and
                                  label.startswith("AM"))
        ok &= hit
        print(f"  {kind:9} -> {label:9} ({conf:.2f}) "
              f"{'OK' if hit else 'MISS  ' + str(f)}")
    print("=" * 56)
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    print(__doc__)
