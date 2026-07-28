"""drm_sweep.py - check candidate DRM frequencies now on air (EiBi DIGITAL
rows). Proper CP metric this time: correlation at each DRM useful-symbol
lag vs a NULL lag nearby - only a true cyclic prefix favors its own Tu."""
import sys
import time
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
import radio_lock

import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32

SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)

FS = 2.048e6
SECS = 40.0
CANDS = [9420e3, 6030e3, 7285e3]        # CNR1 x2 (on air now) + TDF re-check


def cp_score(y, fs2):
    """Return dict mode->ratio of CP-lag correlation vs null-lag."""
    out = {}
    for mode, tu in [("A", 1152), ("B", 1024), ("C", 704), ("D", 448)]:
        def corr(lag):
            prod = y[:-lag] * np.conj(y[lag:])
            m = len(prod) // 4096 * 4096
            return np.abs(prod[:m].reshape(-1, 4096).sum(axis=1)).mean()
        out[mode] = corr(tu) / (corr(tu + 53) + 1e-12)
    return out


with radio_lock.Holder("drm_sweep", "DRM candidate sweep (CNR1/TDF)", 50):
    sdr = None
    for _ in range(18):
        devs = SoapySDR.Device.enumerate("driver=sdrplay")
        if devs:
            try:
                sdr = SoapySDR.Device(devs[0])
                break
            except RuntimeError:
                pass
        time.sleep(5)
    if sdr is None:
        raise SystemExit("[drm] SDR never freed")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
    sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    try:
        sdr.writeSetting("biasT_ctrl", "false")
        sdr.writeSetting("rfgain_sel", "3")
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40)
    except Exception:
        pass
    caps = {}
    for f in CANDS:
        sdr.setFrequency(SOAPY_SDR_RX, 0, f)
        time.sleep(0.4)
        st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        sdr.activateStream(st)
        time.sleep(0.2)
        n = int(FS * SECS)
        buf = np.empty(n, np.complex64)
        got = 0
        chunk = np.empty(131072, np.complex64)
        while got < n:
            r = sdr.readStream(st, [chunk], len(chunk), timeoutUs=int(1e6))
            if r.ret > 0:
                take = min(r.ret, n - got)
                buf[got:got + take] = chunk[:take]
                got += take
        sdr.deactivateStream(st)
        sdr.closeStream(st)
        caps[f] = buf
    del sdr
print("[drm] sweep captured, SDR released", flush=True)

for f, buf in caps.items():
    y = resample_poly(buf, 3, 128).astype(np.complex64)
    fs2 = 48000.0
    (HERE / f"drm_{int(f/1e3)}_48k.cf32").write_bytes(y.tobytes())
    nfft = 8192
    w = np.hanning(nfft)
    segs = [y[i:i + nfft] * w for i in range(0, len(y) - nfft, nfft)]
    psd = np.mean([np.abs(np.fft.fftshift(np.fft.fft(s))) ** 2
                   for s in segs], axis=0)
    fr = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs2))
    inb = np.median(psd[(fr > -4500) & (fr < 4500)])
    outb = np.median(psd[(np.abs(fr) > 8000) & (np.abs(fr) < 16000)])
    occ = 10 * np.log10(inb / outb)
    scores = cp_score(y, fs2)
    best = max(scores, key=scores.get)
    line = " ".join(f"{m}:{v:.2f}" for m, v in scores.items())
    print(f"[drm] {f/1e3:6.0f} kHz: in-band {occ:+5.1f} dB | CP {line} | "
          f"best {best} "
          f"{'<== DRM-LIKE' if scores[best] > 1.5 and occ > 6 else ''}",
          flush=True)
print("[drm] sweep DONE", flush=True)
