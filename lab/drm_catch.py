"""drm_catch.py - catch the 0300z TDF DRM broadcast on 7285 kHz (Caribbean
beam = crosses the US East Coast). Capture 90 s on the loop via the warden,
resample to 48 kHz (DRM OFDM symbols are integer sample counts there), save
for Dream, and fingerprint: 10 kHz flat OFDM block + cyclic-prefix
autocorrelation at each DRM robustness mode's useful-symbol length."""
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
CENTER = 7285e3
SECS = 90.0

with radio_lock.Holder("drm_catch", "TDF 7285 kHz DRM catch (0300z)", 50):
    sdr = None
    for attempt in range(18):               # panel yields cooperatively -
        devs = SoapySDR.Device.enumerate("driver=sdrplay")  # wait it out
        if devs:
            try:
                sdr = SoapySDR.Device(devs[0])
                break
            except RuntimeError:
                pass
        time.sleep(5)
    if sdr is None:
        raise SystemExit("[drm] SDR never freed after 90 s - abort")
    print(f"[drm] device acquired on attempt {attempt + 1}", flush=True)
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setFrequency(SOAPY_SDR_RX, 0, CENTER)
    sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
    sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    try:
        sdr.writeSetting("biasT_ctrl", "false")
        sdr.writeSetting("rfgain_sel", "3")
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40)
    except Exception:
        pass
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(st)
    time.sleep(0.3)
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
    del sdr
print("[drm] captured, SDR released", flush=True)

y = resample_poly(buf, 3, 128).astype(np.complex64)   # 2.048M * 3/128 = 48k
FS2 = 48000.0
(HERE / "drm_tdf7285_48k.cf32").write_bytes(y.tobytes())
print(f"[drm] saved {len(y)/FS2:.0f}s at 48 kHz", flush=True)

# --- fingerprint 1: 10 kHz flat block in the PSD ---------------------------
nfft = 8192
w = np.hanning(nfft)
segs = [y[i:i + nfft] * w for i in range(0, len(y) - nfft, nfft)]
psd = np.mean([np.abs(np.fft.fftshift(np.fft.fft(s))) ** 2 for s in segs],
              axis=0)
fr = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / FS2))
inband = psd[(fr > -4500) & (fr < 4500)]
outband = psd[(np.abs(fr) > 8000) & (np.abs(fr) < 16000)]
occ = 10 * np.log10(np.median(inband) / np.median(outband))
flat = np.percentile(inband, 90) / np.percentile(inband, 10)
print(f"[drm] in-band power {occ:+.1f} dB vs neighborhood, "
      f"flatness p90/p10 {10*np.log10(flat):.1f} dB "
      f"(OFDM is FLAT; AM has a towering carrier)", flush=True)

# --- fingerprint 2: cyclic-prefix autocorrelation at DRM symbol lengths ----
# robustness A/B/C/D useful lengths at 48 kHz: 1152 / 1024 / 704 / 448
best = None
for mode, tu in [("A", 1152), ("B", 1024), ("C", 704), ("D", 448)]:
    prod = y[:-tu] * np.conj(y[tu:])
    m = len(prod) // 4096 * 4096
    mag = np.abs(prod[:m].reshape(-1, 4096).sum(axis=1)).mean()
    ref = np.abs(prod[:m]).mean() * np.sqrt(4096)   # noise expectation
    score = mag / ref
    print(f"[drm]   mode {mode} (Tu={tu}): CP-corr score {score:.2f}",
          flush=True)
    if best is None or score > best[1]:
        best = (mode, score)
print(f"[drm] VERDICT: best mode {best[0]} score {best[1]:.2f} "
      f"({'DRM SIGNATURE' if best[1] > 2.0 else 'no clear DRM'})", flush=True)
