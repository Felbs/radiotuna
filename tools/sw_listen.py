"""sw_listen.py - Radio Tuna: actually LISTEN to a shortwave broadcaster.

Capture N seconds at a guide-chosen frequency, AM-demodulate, write a WAV,
and (optionally) play it. The listening-room proof that the carriers in
the guide are real radio from real places.

Example:  python sw_listen.py --khz 15500 --secs 45 --play
"""
import argparse
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hf_knob import open_sdr, grab, FS   # noqa: E402

LAB = HERE.parent / "lab"
import os as _os
import shutil as _sh
MPV = (_os.environ.get("MPV_EXE") or _sh.which("mpv")
       or r"C:\Program Files\MPV Player\mpv.exe")


def synchronous_am(x, chan_fs):
    """Carrier-locked (synchronous) AM detection. A PLL tracks the carrier
    phase so we demodulate I (in-phase) instead of |x| - this survives
    selective fading, where the carrier fades but sidebands don't and an
    envelope detector garbles. The FPLL idea from the TV decoder, on AM."""
    phase = 0.0
    freq = 0.0
    a, b = 0.01, 0.01 ** 2 / 4
    out = np.empty(len(x), np.float32)
    for i in range(len(x)):
        v = x[i] * (np.cos(-phase) + 1j * np.sin(-phase))
        out[i] = v.real                    # in-phase = the AM audio
        e = v.imag * (1.0 if v.real >= 0 else -1.0)   # phase error
        freq += b * e
        phase += freq + a * e
    return out


def am_demod_wav(iq, out_path, fs=FS, aud=48_000, sync=True):
    """Broadcast-quality AM: narrow channel filter FIRST (Law 4 - the
    signal is ~10 kHz wide, not 250 kHz), then SYNCHRONOUS detection
    (carrier-locked, beats envelope under selective fading), then
    voice-band shaping and a slow AGC that rides shortwave's fades."""
    from scipy.signal import resample_poly, butter, sosfilt
    from math import gcd
    # 1. channel filter: decimate 250k -> 12.5k (+-6.25 kHz) - this alone
    #    removes ~13 dB of out-of-channel noise vs raw-envelope
    chan_fs = 12_500
    g = gcd(chan_fs, int(fs))
    x = resample_poly(iq, chan_fs // g, int(fs) // g).astype(np.complex64)
    x = x - np.mean(x)
    if sync:
        env = synchronous_am(x, chan_fs)
    else:
        env = np.abs(x).astype(np.float32)
    env -= float(np.mean(env))
    # 2. voice-band shaping: 100 Hz - 4.5 kHz bandpass
    sos = butter(4, [100, 4500], btype="band", fs=chan_fs, output="sos")
    env = sosfilt(sos, env).astype(np.float32)
    # 2b. rescue chain (default on, RT_AM_RESCUE=0 for the raw chain):
    #     - hum comb notch: broadcast transmitters leak PSU ripple as
    #       AM sidebands at the 60 Hz family (measured +28 dB line on a
    #       50 kW station; notching it cut the buzz 23 dB)
    #     - gentle Wiener NR profiled on the pause floor (12th pct)
    import os
    if os.environ.get("RT_AM_RESCUE", "1") != "0":
        from scipy.signal import iirnotch, tf2sos, stft, istft
        for f0 in (120.0, 240.0, 180.0, 360.0):
            b_n, a_n = iirnotch(f0, Q=25.0, fs=chan_fs)
            env = sosfilt(tf2sos(b_n, a_n), env).astype(np.float32)
        _f, _t, Z = stft(env, fs=chan_fs, nperseg=1024, noverlap=768)
        mag2 = np.abs(Z) ** 2
        npsd = np.percentile(mag2, 12, axis=1, keepdims=True)
        H = np.maximum(1.0 - 1.2 * npsd / np.maximum(mag2, 1e-18), 0.25)
        _, env = istft(Z * H, fs=chan_fs, nperseg=1024, noverlap=768)
        env = env.astype(np.float32)
    # 3. slow AGC (1 s window) - rides QSB fades instead of letting the
    #    whole clip breathe up and down
    k = chan_fs  # 1 s
    p = np.convolve(env ** 2, np.ones(k, np.float32) / k, mode="same")
    gain = 0.25 / (np.sqrt(p) + 1e-4)
    gain = np.clip(gain, 0, 60.0)
    env = np.clip(env * gain, -0.95, 0.95)
    # 4. up to 48k for the WAV
    g2 = gcd(int(aud), chan_fs)
    audio = resample_poly(env, int(aud) // g2, chan_fs // g2).astype(np.float32)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(aud)
        w.writeframes(pcm.tobytes())
    return len(pcm) / aud


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--khz", type=float, required=True)
    ap.add_argument("--secs", type=float, default=45)
    ap.add_argument("--antenna", default="Antenna C")
    ap.add_argument("--play", action="store_true")
    args = ap.parse_args()
    print(f"[listen] tuning {args.khz:.0f} kHz on {args.antenna}, "
          f"{args.secs:.0f}s ...")
    sdr, st = open_sdr(args.antenna)
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX
    sdr.setFrequency(SOAPY_SDR_RX, 0, args.khz * 1e3)
    time.sleep(0.2)
    iq = grab(sdr, st, args.secs)
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    print(f"[listen] captured {len(iq)/FS:.1f}s - SDR released. demodulating ...")
    out = LAB / f"sw_{int(args.khz)}.wav"
    dur = am_demod_wav(iq, out)
    print(f"[listen] wrote {out} ({dur:.0f}s)")
    if args.play and Path(MPV).exists():
        print("[listen] playing through speakers ...")
        subprocess.Popen([MPV, str(out), "--volume=90", "--force-window=no"])
    return out


if __name__ == "__main__":
    main()
