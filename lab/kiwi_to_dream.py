"""kiwi_to_dream.py - bridge kiwirecorder IQ wav captures to the Dream DRM receiver.

kiwirecorder.py -m iq (github.com/jks-prv/kiwiclient, credit jks-prv et al.)
writes stereo 16-bit wav, I=left / Q=right, ~12 kHz rate, signal at 0 Hz.
Dream 2.2 win32 (drm.sourceforge.io, GPL-2.0, Volker Fischer et al. /
TU Darmstadt) was bench-validated 2026-07-28 on two file-input paths
(see drm_to_wav.py / drm_day_log.md):

  * mono 12 kHz-IF real wav, default input  <- THE path (used here)
  * stereo IQ wav with --inchansel 8

The Kiwi's 12 kHz rate is too low for Dream's soundcard-rate assumptions,
so we upsample x4 to 48 kHz (polyphase), mix to a +12 kHz IF, take the
real part and write mono 16-bit 48 kHz. The Kiwi's true rate is ~12001 Hz
(header says 11999); treating it as 12000 leaves a <100 ppm error that
Dream's tracking absorbs.

Usage:
  python kiwi_to_dream.py capture.wav [--out out_if12.wav] [--iq]

  --iq   also write a 48 kHz stereo IQ wav (feed Dream with -c 8)

Then:
  Z:\\src\\dream\\console\\dream.exe -f capture_if12.wav -w decoded.wav
"""
import argparse
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly


def load_kiwi_iq_wav(path):
    with wave.open(str(path), "rb") as w:
        fs = w.getframerate()
        ch = w.getnchannels()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if ch != 2:
        raise SystemExit(f"[kiwi2dream] {path}: expected stereo IQ wav, got {ch} ch")
    z = (raw[0::2].astype(np.float64) + 1j * raw[1::2].astype(np.float64)) / 32768.0
    return z, fs


def normalize(x, headroom_db=6.0):
    p = np.percentile(np.abs(x), 99.9)
    if p <= 0:
        return x
    return x * (10 ** (-headroom_db / 20) / p)


def write_wav(path, channels, fs):
    data = np.stack(channels, axis=-1) if len(channels) > 1 else channels[0]
    pcm = np.clip(data * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(len(channels))
        w.setsampwidth(2)
        w.setframerate(int(fs))
        w.writeframes(pcm.tobytes())
    print(f"[kiwi2dream] wrote {path} ({len(channels)} ch, {int(fs)} Hz, "
          f"{data.shape[0]/fs:.1f} s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wav", help="kiwirecorder stereo IQ wav (~12 kHz)")
    ap.add_argument("--out", default=None, help="output path (default <in>_if12.wav)")
    ap.add_argument("--iq", action="store_true", help="also write 48 kHz stereo IQ wav")
    args = ap.parse_args()

    src = Path(args.wav)
    z, fs = load_kiwi_iq_wav(src)
    print(f"[kiwi2dream] {src.name}: {len(z)/fs:.1f} s at {fs} Hz (treating as 12000)")
    z = normalize(z - z.mean())
    z48 = resample_poly(z, 4, 1)   # 12 kHz -> 48 kHz
    fs48 = 48000.0

    n = np.arange(len(z48))
    shifted = z48 * np.exp(2j * np.pi * 12000.0 / fs48 * n)
    real = normalize(shifted.real)
    out = Path(args.out) if args.out else src.with_name(src.stem + "_if12.wav")
    write_wav(out, [real], fs48)

    if args.iq:
        write_wav(src.with_name(src.stem + "_iq48.wav"),
                  [normalize(z48.real), normalize(z48.imag)], fs48)


if __name__ == "__main__":
    main()
