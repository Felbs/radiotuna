"""drm_to_wav.py - bridge our cf32 SDR captures to the Dream DRM receiver.

Dream (https://drm.sourceforge.io, GPL-2.0 - credit: Volker Fischer et al.,
Darmstadt University of Technology) takes FILE input via `-f file.wav`.
Two input conventions both work; we can emit either:

  * iq   : stereo 16-bit WAV, I=left / Q=right, signal at 0 Hz.
           Feed Dream with `--inchansel 8` (I/Q positive SPLIT).
           VALIDATED 2026-07-28 vs the official DW_ModeB_10kHz sample:
           modes 4/5/6/7 never sync in dream-2.2 win32; mode 8 does.
  * if12 : real mono 16-bit WAV, signal mixed up to a +12 kHz IF
           (classic "radio audio out" convention, Dream's default
           mix-channel input; freq acquisition window centers fs/4).
           Also validated - syncs and decodes audio.

Sample rate is preserved (our captures are 48 kHz, where DRM symbol
lengths are integer sample counts: Tu A/B/C/D = 1152/1024/704/448).

Usage:
  python drm_to_wav.py capture.cf32 [--mode iq|if12|both] [--fs 48000]
                       [--out basename]

Example decode afterwards (note doubled backslashes for the docstring):
  Z:\\src\\dream\\console\\dream.exe -f capture_iq.wav -c 8 -w decoded.wav
"""
import argparse
import struct
import wave
from pathlib import Path

import numpy as np


def load_cf32(path):
    raw = np.fromfile(path, dtype=np.complex64)
    if raw.size == 0:
        raise SystemExit(f"[bridge] {path}: empty file")
    return raw


def normalize(x, headroom_db=6.0):
    """Scale so the 99.9th-percentile magnitude sits headroom_db below FS."""
    p = np.percentile(np.abs(x), 99.9)
    if p <= 0:
        return x
    return x * (10 ** (-headroom_db / 20) / p)


def write_wav(path, channels, fs):
    """channels: list of float arrays in [-1, 1]; 16-bit PCM out."""
    data = np.stack(channels, axis=-1) if len(channels) > 1 else channels[0]
    pcm = np.clip(data * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(len(channels))
        w.setsampwidth(2)
        w.setframerate(int(fs))
        w.writeframes(pcm.tobytes())
    print(f"[bridge] wrote {path} ({len(channels)} ch, {int(fs)} Hz, "
          f"{data.shape[0]/fs:.1f} s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cf32", help="complex64 capture file")
    ap.add_argument("--mode", choices=["iq", "if12", "both"], default="both")
    ap.add_argument("--fs", type=float, default=48000.0)
    ap.add_argument("--out", default=None, help="output basename")
    args = ap.parse_args()

    src = Path(args.cf32)
    base = Path(args.out) if args.out else src.with_suffix("")
    y = normalize(load_cf32(src))
    print(f"[bridge] {src.name}: {len(y)/args.fs:.1f} s at {args.fs:.0f} Hz")

    if args.mode in ("iq", "both"):
        write_wav(base.parent / (base.name + "_iq.wav"),
                  [y.real.astype(np.float64), y.imag.astype(np.float64)],
                  args.fs)

    if args.mode in ("if12", "both"):
        n = np.arange(len(y))
        shifted = y * np.exp(2j * np.pi * 12000.0 / args.fs * n)
        real = normalize(shifted.real.astype(np.float64), headroom_db=6.0)
        write_wav(base.parent / (base.name + "_if12.wav"), [real], args.fs)


if __name__ == "__main__":
    main()
