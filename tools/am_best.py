"""am_best.py - Radio Tuna: the mathematically-best AM/shortwave chain.

The fm_stereo-v2 move, replayed on amplitude modulation: take every
mechanism that measurably bought decibels in the lab and stack them into
ONE production demodulator, each stage justified by a selftest number,
none by vibes.

The stages (and where each dB was proven):
  1. carrier NAIL + narrowband filter-lock  - sync detection survives
     selective fading where envelope folds over (sync_am selftest:
     +25 dB during a -20 dB carrier notch)
  2. sideband-diversity MRC                 - AM transmits the audio
     twice (mirror sidebands); per-block SNR weights slide to the clean
     copy under splatter/selective notches (sync_am selftest: +14 dB
     with one sideband jammed, lossless when symmetric)
  3. adaptive audio bandwidth               - measure the per-band audio
     SNR and low-pass at the frequency where program stops beating the
     noise; wideband when clean, narrow when hissy (the FM adaptive-
     blend idea, applied to bandwidth)
  4. hum comb notch                         - PSU ripple rides big
     transmitters as 60 Hz-family AM sidebands (measured +28 dB line on
     a 50 kW station; notching cut the buzz 23 dB)
  5. Wiener noise reduction                 - gentle, profiled on the
     pause floor (12th percentile), never below 0.25 gain
  6. slow AGC                               - rides QSB fades without
     letting the clip breathe

Chunk-safe: call best_chunk() repeatedly with a state dict and the AGC /
bandwidth choices carry over - this is what am_listen's live loop uses.
Every call returns a diagnostics dict: the truth dial for the panel.

  python am_best.py selftest      # prove every stage, no radio needed
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from scipy.signal import (firwin, filtfilt, lfilter, resample_poly,
                          iirnotch, tf2sos, sosfilt, stft, istft)

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CARRIER_BW = 30.0        # Hz - carrier-tracking filter
DIV_BLK_S = 0.25         # s  - MRC weight block
CUT_MIN, CUT_MAX = 2600.0, 5200.0
CUT_SNR_DB = 3.0         # program must beat noise by this to keep a band


def nail_carrier(x, fs):
    """Exact residual carrier offset (Hz) by averaged FFT peak near DC.
    Also counts DISTINCT carrier lines within +-250 Hz: real transmitters
    sit fractions of Hz off the raster, so every co-channel station shows
    as its own line - the classic MW-DX offset-counting trick, now a
    dial. Returns (offset_hz, carrier_snr_db, n_cochannel)."""
    n = 1 << max(12, min(15, int(np.log2(max(len(x), 4096)))))
    n = min(n, len(x))
    segs = np.abs(np.fft.fftshift(
        np.fft.fft(x[:len(x) // n * n].reshape(-1, n)
                   * np.hanning(n).astype(np.float32), axis=1), axes=1)) ** 2
    P = segs.mean(axis=0)
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / fs))
    m = np.abs(f) < 500.0
    # carrier prominence = line power over the local floor -> the SNR dial
    pk = float(P[m].max())
    floor = float(np.median(P[m]))
    # co-channel census on the MIN-hold PSD: a co-channel carrier is a
    # line that persists through every segment; program bass sidebands
    # flicker with the modulation and drop out of the minimum. (The
    # mean-PSD census counted a local blowtorch's bass as 5 stations.)
    Pmin = segs.min(axis=0) if len(segs) > 1 else P
    mm = np.abs(f) < 250.0
    Pm, fm = Pmin[mm].copy(), f[mm]
    floor_min = float(np.median(Pmin[m]))
    thresh = floor_min * 10 ** 1.4
    lines = []
    powers = []
    while True:
        i = int(np.argmax(Pm))
        if Pm[i] < thresh or len(lines) >= 5:
            break
        lines.append(float(fm[i]))
        powers.append(float(Pm[i]))
        Pm[np.abs(fm - fm[i]) < 4.0] = 0.0
    # dominance: how far the loudest RIVAL line sits below the main
    # carrier - five buried co-channels are harmless, one at -8 dB is
    # a ruined channel. This is the number quality should care about.
    dom = 99.0
    if len(powers) >= 2:
        dom = 10 * np.log10(max(powers[0], 1e-18) / max(powers[1], 1e-18))
    return (f[m][np.argmax(P[m])], 10 * np.log10(pk / max(floor, 1e-18)),
            max(1, len(lines)), round(float(dom), 1))


def _sidebands(y, fs):
    """USB/LSB audio branches from a carrier-locked signal (validated
    diversity front-end: modulate the prototype about its CENTER tap or
    a passband phase rotation mixes audio into its Hilbert twin)."""
    lp = firwin(513, 2450.0 / (fs / 2))
    k = np.arange(len(lp)) - (len(lp) - 1) / 2
    h_u = (lp * np.exp(2j * np.pi * 2550.0 / fs * k)).astype(np.complex64)
    h_l = (lp * np.exp(-2j * np.pi * 2550.0 / fs * k)).astype(np.complex64)
    U = 2.0 * np.convolve(y, h_u, mode="same").real.astype(np.float32)
    L = 2.0 * np.convolve(y, h_l, mode="same").real.astype(np.float32)
    return U, L


def _mrc(U, L, fs):
    """Per-block sideband MRC. Shared component = signal by construction
    (cross-covariance); per-branch noise = var - S; weights 1/N."""
    blk = max(1, int(DIV_BLK_S * fs))
    nblk = max(1, len(U) // blk)
    wU = np.empty(nblk, np.float32)
    wL = np.empty(nblk, np.float32)
    for i in range(nblk):
        u = U[i * blk:(i + 1) * blk]
        l = L[i * blk:(i + 1) * blk]
        S = max(float(np.mean(u * l)), 1e-12)
        wU[i] = 1.0 / max(float(np.var(u)) - S, S * 1e-2)
        wL[i] = 1.0 / max(float(np.var(l)) - S, S * 1e-2)
    tilt = 10 * np.log10(float(np.mean(1 / wU) / np.mean(1 / wL)))

    def expand(w):
        w = np.convolve(w, np.ones(3, np.float32) / 3, mode="same")
        e = np.repeat(w, blk)
        return np.pad(e, (0, max(0, len(U) - len(e))), mode="edge")[:len(U)]
    eU, eL = expand(wU), expand(wL)
    return (eU * U + eL * L) / (eU + eL), tilt


def _adaptive_cutoff(y, fs, state):
    """Physically-honest bandwidth choice. Percentile tricks on the audio
    PSD lie (pure noise has a FIXED p60/p12 ratio, so noise 'beats'
    noise); instead measure the channel: noise density N0 comes from
    OUTSIDE the AM channel (|f| > 5.5 kHz - no sideband can live there),
    program power from the folded +-sidebands. Keep audio up to the last
    band where the fold clears the noise fold by CUT_SNR_DB."""
    n = min(8192, len(y))
    seg = y[:len(y) // n * n].reshape(-1, n) * np.hanning(n).astype(np.float32)
    P = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / fs))
    hi = min(0.45 * fs, 9000.0)
    nz = (np.abs(f) > 5500.0) & (np.abs(f) < hi)
    # 15th percentile, not median: on real bands the out-of-channel
    # region CONTAINS the adjacent stations (10 kHz AM / 5 kHz SW
    # spacing) - true noise lives in the quiet bins between them
    N0 = float(np.percentile(P[nz], 15)) if nz.any() else float(np.median(P))
    pos = f > 0
    fp = f[pos]
    fold = P[pos] + np.interp(-fp, f, P)          # USB + LSB power per band
    # 300 Hz smoothing so a lone splatter line can't buy bandwidth
    kb = max(1, int(300.0 / (fs / n)))
    smooth = np.convolve(fold, np.ones(kb) / kb, mode="same")
    keep = (fp > 300) & (fp < CUT_MAX) & (smooth > 2 * N0 * 10 ** (CUT_SNR_DB / 10.0) + 2 * N0)
    cut = float(fp[keep].max()) if keep.any() else CUT_MIN
    cut = float(np.clip(cut, CUT_MIN, CUT_MAX))
    prev = state.get("cutoff", cut)
    cut = 0.6 * prev + 0.4 * cut          # no per-chunk bandwidth flapping
    state["cutoff"] = cut
    # honest audio SNR: program power over noise, averaged across the
    # voice band actually kept (the same physics the cutoff came from)
    vb = (fp > 300) & (fp < max(cut, CUT_MIN))
    if vb.any():
        prog = np.maximum(smooth[vb] - 2 * N0, 2 * N0 * 1e-3)
        asnr = float(10 * np.log10(np.mean(prog) / (2 * N0)))
    else:
        asnr = 0.0
    return cut, asnr


def best_chunk(x, fs, state=None, rescue=True):
    """The whole chain on one complex chunk at channel rate `fs`
    (>= 12.5 kHz, carrier at ~DC). Returns (audio at fs, diag dict).
    Pass the same `state` dict every call for AGC/bandwidth continuity."""
    if state is None:
        state = {}
    diag = {}
    # 1. carrier nail + filter-lock (vectorized PLL)
    doff, csnr, ncoch, dom = nail_carrier(x, fs)
    diag["cochannel"] = ncoch
    diag["coch_dom_db"] = dom
    n = np.arange(len(x), dtype=np.float64)
    x = x * np.exp(-2j * np.pi * doff / fs * n).astype(np.complex64)
    c = filtfilt(firwin(2049, CARRIER_BW / (fs / 2)), [1.0], x)
    amp = np.abs(c).astype(np.float32)
    y = (x * np.conj(c / np.maximum(amp, 1e-12))).astype(np.complex64)
    med = float(np.median(amp))
    fade_db = 20 * np.log10(np.maximum(amp, 1e-12) / max(med, 1e-12))
    diag["carrier_snr_db"] = float(round(csnr, 1))
    diag["fade_frac6"] = round(float((fade_db < -6).mean()), 3)
    diag["fade_min_db"] = round(float(fade_db.min()), 1)

    # 2. sideband-diversity MRC
    U, L = _sidebands(y, fs)
    a, tilt = _mrc(U, L, fs)
    diag["tilt_db"] = float(round(tilt, 1))

    # DC / rumble block
    hp = firwin(2049, 60.0 / (fs / 2))
    a = (a - filtfilt(hp, [1.0], a)).astype(np.float32)

    # 4. hum comb (measure 120 Hz line first so the dial is honest)
    nseg = 4096 if len(a) >= 8192 else 1024
    F = np.abs(np.fft.rfft(a[:len(a) // nseg * nseg].reshape(-1, nseg),
                           axis=1)).mean(axis=0) ** 2
    fr = np.fft.rfftfreq(nseg, 1 / fs)
    i120 = int(np.argmin(np.abs(fr - 120.0)))
    hood = F[max(0, i120 - 20):i120 + 20]
    diag["hum_db"] = float(round(10 * np.log10(F[i120] / max(float(np.median(hood)), 1e-18)), 1))
    if rescue:
        for f0 in (120.0, 240.0, 180.0, 360.0):
            b_n, a_n = iirnotch(f0, Q=25.0, fs=fs)
            a = sosfilt(tf2sos(b_n, a_n), a).astype(np.float32)

    # 4b. heterodyne excision (the hd_excision lever, audio edition):
    # co-channel carriers a few hundred Hz off beat into stationary
    # whistles. A program tone comes and goes; a het holds the whole
    # chunk - so detect lines in the MINIMUM per-segment PSD (present in
    # every 0.4 s slice) and notch them surgically.
    hets = []
    if rescue and len(a) >= 2 * 8192:
        nseg = 8192
        segs = np.abs(np.fft.rfft(a[:len(a) // nseg * nseg].reshape(-1, nseg),
                                  axis=1)) ** 2
        fr2 = np.fft.rfftfreq(nseg, 1 / fs)
        Pmin = segs.min(axis=0)
        band = (fr2 > 400) & (fr2 < 4800)
        med2 = float(np.median(Pmin[band]))
        cand = Pmin.copy()
        cand[~band] = 0.0
        while len(hets) < 3:
            i = int(np.argmax(cand))
            if cand[i] < med2 * 10 ** 1.6:      # 16 dB over the hold-floor
                break
            hets.append(float(fr2[i]))
            cand[np.abs(fr2 - fr2[i]) < 30.0] = 0.0
        for f0 in hets:
            b_n, a_n = iirnotch(f0, Q=60.0, fs=fs)
            a = sosfilt(tf2sos(b_n, a_n), a).astype(np.float32)
    diag["hets_hz"] = [int(h) for h in hets]

    # 3. adaptive bandwidth (measured on the carrier-locked CHANNEL, where
    # out-of-channel noise density is observable)
    cut, audio_snr = _adaptive_cutoff(y, fs, state)
    diag["cutoff_hz"] = int(cut)
    diag["audio_snr_db"] = round(audio_snr, 1)
    a = filtfilt(firwin(257, cut / (fs / 2)), [1.0], a).astype(np.float32)

    # 5. Wiener NR on the pause floor
    if rescue and len(a) >= 4096:
        _f, _t, Z = stft(a, fs=fs, nperseg=1024, noverlap=768)
        mag2 = np.abs(Z) ** 2
        npsd = np.percentile(mag2, 12, axis=1, keepdims=True)
        H = np.maximum(1.0 - 1.2 * npsd / np.maximum(mag2, 1e-18), 0.25)
        _, a = istft(Z * H, fs=fs, nperseg=1024, noverlap=768)
        a = a.astype(np.float32)

    # 6. slow AGC with cross-chunk state
    k = int(fs)
    p = np.convolve(a ** 2, np.ones(k, np.float32) / k, mode="same")
    tgt = np.sqrt(p) + 1e-4
    agc = state.get("agc", float(tgt.mean()))
    # one-pole smoothing toward this chunk's level, sample-continuous
    lam = np.exp(-1.0 / (2.0 * fs))          # 2 s time constant
    smooth = lfilter([1 - lam], [1, -lam], tgt, zi=[agc * lam])[0]
    state["agc"] = float(smooth[-1])
    a = np.clip(a * (0.25 / np.maximum(smooth, 1e-4)), -0.95, 0.95)
    diag["level"] = round(float(np.sqrt(np.mean(a ** 2))), 3)

    # the AUDIO QUALITY score - the STVT watchability law on radio:
    # quality = delivery x (1 - errors), every term measured above.
    # base: sigmoid on honest audio SNR (5 dB ~ 27, 12 ~ 50, 22 ~ 81)
    base = 100.0 / (1.0 + np.exp(-(audio_snr - 12.0) / 7.0))
    delivery = 1.0 - min(1.0, diag["fade_frac6"] * 2.5)   # fades steal words
    fidelity = 0.85 + 0.15 * (cut / CUT_MAX)              # bandwidth = warmth
    q = base * delivery * fidelity
    # co-channel penalty rides DOMINANCE, not headcount: five rivals
    # buried 30 dB down are inaudible; one at 8 dB ruins the channel
    q -= (20 if dom < 8 else 10 if dom < 15 else 4 if dom < 22 else 0)
    q -= 2 * len(hets)                                    # notched, scarred
    q = int(np.clip(q, 0, 100))
    diag["quality"] = q
    diag["grade"] = ("EXCELLENT" if q >= 75 else "GOOD" if q >= 55 else
                     "FAIR" if q >= 35 else "ROUGH" if q >= 18 else "STATIC")
    return a.astype(np.float32), diag


def demod_wav(iq, out_path, fs, aud=48_000, chan_fs=20_000):
    """Batch capture -> best-chain WAV (sw_listen's new engine)."""
    import wave
    from math import gcd
    g = gcd(int(chan_fs), int(fs))
    x = resample_poly(iq, int(chan_fs) // g, int(fs) // g).astype(np.complex64)
    x = x - np.mean(x)
    a, diag = best_chunk(x, chan_fs)
    g2 = gcd(int(aud), int(chan_fs))
    audio = resample_poly(a, int(aud) // g2, int(chan_fs) // g2)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(aud)
        w.writeframes(pcm.tobytes())
    return len(pcm) / aud, diag


# ----------------------------------------------------------------------
# theory bench - every stage must pay its way (fm_theory_bench law)
# ----------------------------------------------------------------------
def _out_snr(a, ref, sel):
    aa, rr = a[sel], ref[sel]
    g = np.dot(aa, rr) / max(np.dot(rr, rr), 1e-12)
    err = aa - g * rr
    return 10 * np.log10(np.dot(rr, rr) * g ** 2 / max(np.dot(err, err), 1e-12))


def cmd_selftest():
    fs = 20_000.0
    rng = np.random.default_rng(7)
    t = np.arange(int(20 * fs)) / fs
    m = (0.4 * np.sin(2 * np.pi * 440 * t) + 0.25 * np.sin(2 * np.pi * 1100 * t)
         + 0.15 * np.sin(2 * np.pi * 2500 * t)).astype(np.float32)
    ref = filtfilt(firwin(257, 5000 / (fs / 2)), [1.0], m).astype(np.float32)
    sel = np.zeros(len(t), bool)
    sel[int(2 * fs):int(18 * fs)] = True
    ok = True
    print("=" * 64)
    print("am_best theory bench - each stage must pay its way")
    print("=" * 64)

    def env_detect(y):
        e = np.abs(y).astype(np.float32)
        e = e - filtfilt(firwin(2049, 60.0 / (fs / 2)), [1.0], e)
        return filtfilt(firwin(257, 5000 / (fs / 2)), [1.0], e).astype(np.float32)

    # A. selective carrier fade -20 dB: best must crush envelope
    s = (1.0 + m).astype(np.complex64)
    g = np.zeros(len(t), np.float32)
    g[(t > 5) & (t < 15)] = 0.9
    y = s - g * 1.0
    y += (rng.normal(0, .01, len(y)) + 1j * rng.normal(0, .01, len(y))).astype(np.complex64)
    b, d = best_chunk(y.copy(), fs, rescue=False)
    fade_sel = sel & (t > 6) & (t < 14)
    sb, se = _out_snr(b, ref, fade_sel[:len(b)]), _out_snr(env_detect(y), ref, fade_sel)
    print(f"  A carrier -20dB : envelope {se:5.1f} dB  BEST {sb:5.1f} dB  "
          f"-> +{sb - se:.1f} dB  (fade dial {d['fade_min_db']} dB)")
    ok &= (sb - se) > 6

    # B. one-sided splatter: MRC must beat plain DSB sync
    y2 = s + (rng.normal(0, .01, len(t)) + 1j * rng.normal(0, .01, len(t))).astype(np.complex64)
    ni = (rng.normal(0, 1, len(t)) + 1j * rng.normal(0, 1, len(t))).astype(np.complex64)
    ni = filtfilt(firwin(513, 1500 / (fs / 2)), [1.0], ni)
    ni *= np.exp(2j * np.pi * 2500 / fs * np.arange(len(t)))
    y2 = (y2 + 0.35 * ni).astype(np.complex64)
    b2, d2 = best_chunk(y2.copy(), fs, rescue=False)
    # plain DSB sync = equal-weight sidebands on the same front-end
    doff, _, _, _ = nail_carrier(y2, fs)
    yc = y2 * np.exp(-2j * np.pi * doff / fs * np.arange(len(y2))).astype(np.complex64)
    c = filtfilt(firwin(2049, CARRIER_BW / (fs / 2)), [1.0], yc)
    ycl = (yc * np.conj(c / np.maximum(np.abs(c), 1e-12))).astype(np.complex64)
    U, L = _sidebands(ycl, fs)
    dsb = 0.5 * (U + L)
    dsb = dsb - filtfilt(firwin(2049, 60 / (fs / 2)), [1.0], dsb)
    dsb = filtfilt(firwin(257, 5000 / (fs / 2)), [1.0], dsb).astype(np.float32)
    s2b, s2d = _out_snr(b2, ref, sel[:len(b2)]), _out_snr(dsb, ref, sel)
    print(f"  B USB jammed    : DSB sync {s2d:5.1f} dB  BEST {s2b:5.1f} dB  "
          f"-> +{s2b - s2d:.1f} dB  (tilt dial {d2['tilt_db']} dB)")
    ok &= (s2b - s2d) > 4

    # C. clean symmetric: the chain must do no harm
    y3 = s + (rng.normal(0, .02, len(t)) + 1j * rng.normal(0, .02, len(t))).astype(np.complex64)
    b3, d3 = best_chunk(y3.copy(), fs, rescue=False)
    doff, _, _, _ = nail_carrier(y3, fs)
    yc = y3 * np.exp(-2j * np.pi * doff / fs * np.arange(len(y3))).astype(np.complex64)
    c = filtfilt(firwin(2049, CARRIER_BW / (fs / 2)), [1.0], yc)
    sync = (yc * np.conj(c / np.maximum(np.abs(c), 1e-12))).real.astype(np.float32)
    sync = sync - filtfilt(firwin(2049, 60 / (fs / 2)), [1.0], sync)
    sync = filtfilt(firwin(257, 5000 / (fs / 2)), [1.0], sync).astype(np.float32)
    s3b, s3s = _out_snr(b3, ref, sel[:len(b3)]), _out_snr(sync, ref, sel)
    print(f"  C clean signal  : sync {s3s:5.1f} dB  BEST {s3b:5.1f} dB  "
          f"(must never be worse)")
    ok &= s3b > s3s - 1.0

    # D. transmitter hum: comb must measurably cut the 120 Hz family
    hum = 0.15 * np.sin(2 * np.pi * 120 * t) + 0.08 * np.sin(2 * np.pi * 240 * t)
    y4 = ((1.0 + m + hum) + 0j).astype(np.complex64)
    y4 += (rng.normal(0, .01, len(t)) + 1j * rng.normal(0, .01, len(t))).astype(np.complex64)
    _b4r, d4_raw = best_chunk(y4.copy(), fs, rescue=False)
    b4, d4 = best_chunk(y4.copy(), fs, rescue=True)

    def hum_power(a):
        nseg = 4096
        F = np.abs(np.fft.rfft(a[:len(a) // nseg * nseg].reshape(-1, nseg),
                               axis=1)).mean(axis=0) ** 2
        fr = np.fft.rfftfreq(nseg, 1 / fs)
        return 10 * np.log10(F[int(np.argmin(np.abs(fr - 120.0)))])
    cut_db = hum_power(_b4r) - hum_power(b4)
    print(f"  D 120Hz hum     : dial reads {d4_raw['hum_db']} dB line; "
          f"comb cuts {cut_db:.1f} dB")
    ok &= d4_raw["hum_db"] > 6 and cut_db > 10

    # E. adaptive bandwidth: same WIDEBAND program (music stand-in =
    # band-limited noise to 4.8 kHz), clean channel must keep the highs,
    # hissy channel must narrow to protect intelligibility
    mw = filtfilt(firwin(513, 4800 / (fs / 2)), [1.0],
                  rng.normal(0, 0.35, len(t))).astype(np.float32)
    sw_sig = (1.0 + np.clip(mw, -0.95, 0.95)).astype(np.complex64)
    yn = sw_sig + (rng.normal(0, .25, len(t)) + 1j * rng.normal(0, .25, len(t))).astype(np.complex64)
    ycl2 = sw_sig + (rng.normal(0, .005, len(t)) + 1j * rng.normal(0, .005, len(t))).astype(np.complex64)
    _bn, dn = best_chunk(yn.copy(), fs, rescue=False)
    _bc, dc = best_chunk(ycl2.copy(), fs, rescue=False)
    print(f"  E adaptive BW   : hissy picks {dn['cutoff_hz']} Hz, "
          f"clean picks {dc['cutoff_hz']} Hz")
    ok &= dn["cutoff_hz"] < dc["cutoff_hz"] - 800

    # F. heterodyne whistle: a co-channel carrier beat is STATIONARY;
    # program isn't. Gate the program on/off so only the het holds, and
    # the excisor must find and cut it.
    gate = ((t % 4.0) < 2.0).astype(np.float32)
    m6 = (m * gate).astype(np.float32)
    het = 0.22 * np.sin(2 * np.pi * 1350.0 * t).astype(np.float32)
    y6 = ((1.0 + m6 + het) + 0j).astype(np.complex64)
    y6 += (rng.normal(0, .01, len(t)) + 1j * rng.normal(0, .01, len(t))).astype(np.complex64)
    b6r, _d6r = best_chunk(y6.copy(), fs, rescue=False)
    b6, d6 = best_chunk(y6.copy(), fs, rescue=True)

    def tone_power(a, f0):
        nseg = 8192
        F6 = np.abs(np.fft.rfft(a[:len(a) // nseg * nseg].reshape(-1, nseg),
                                axis=1)).mean(axis=0) ** 2
        fr6 = np.fft.rfftfreq(nseg, 1 / fs)
        return 10 * np.log10(F6[int(np.argmin(np.abs(fr6 - f0)))])
    cut6 = tone_power(b6r, 1350.0) - tone_power(b6, 1350.0)
    found = d6["hets_hz"] and abs(d6["hets_hz"][0] - 1350) < 20
    print(f"  F 1350Hz het    : excisor found {d6['hets_hz']} Hz, "
          f"cut {cut6:.1f} dB")
    ok &= bool(found) and cut6 > 10

    # G. the quality metric must rank like an ear: clean wideband >
    # hissy > deep-faded, with sane absolute placements
    _bq, dq_clean = best_chunk(ycl2.copy(), fs, rescue=False)
    _bq2, dq_hiss = best_chunk(yn.copy(), fs, rescue=False)
    _bq3, dq_fade = best_chunk(y.copy(), fs, rescue=False)
    print(f"  G quality metric: clean {dq_clean['quality']} ({dq_clean['grade']})"
          f"  hissy {dq_hiss['quality']} ({dq_hiss['grade']})"
          f"  faded {dq_fade['quality']} ({dq_fade['grade']})")
    ok &= (dq_clean["quality"] > dq_hiss["quality"] > dq_fade["quality"]
           and dq_clean["quality"] >= 70 and dq_fade["quality"] <= 45)

    print("=" * 64)
    print("SELFTEST", "PASS" if ok else "FAIL")
    print("=" * 64)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    args = ap.parse_args()
    if args.cmd == "selftest":
        sys.exit(cmd_selftest())


if __name__ == "__main__":
    main()
