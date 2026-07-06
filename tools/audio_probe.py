"""audio_probe.py — the audio liveness dial: is this WAV carrying real
program audio, or noise/silence? (TV Tuna's liveness law, ported to
sound: a growing file proves nothing until its CONTENT is verified.)

Metrics on the last few seconds:
  spectral flatness  — white noise ~= 1.0; music/speech < ~0.5
  crest factor       — noise ~3x rms; music/speech dynamics run higher
  band tilt          — real audio concentrates energy low; static doesn't
Verdict: MUSIC/SPEECH, STATIC, SILENCE, or CLIPPED.

Usage: python audio_probe.py <file.wav> [tail_seconds]
"""
import struct
import sys

import numpy as np


def read_wav_tail(path, secs=3.0):
    with open(path, "rb") as f:
        hdr = f.read(64)
        if hdr[:4] != b"RIFF":
            raise ValueError("not a WAV")
        # find fmt chunk (assume standard offsets when sane)
        ch = hdr.find(b"fmt ")
        n_ch, rate = struct.unpack("<HI", hdr[ch + 10:ch + 16])
        bits = struct.unpack("<H", hdr[ch + 22:ch + 24])[0]
        data = hdr.find(b"data")
        data_start = data + 8 if data > 0 else 44
        f.seek(0, 2)
        end = f.tell()
        frame = n_ch * bits // 8
        n = int(secs * rate) * frame
        f.seek(max(data_start, end - n))
        raw = f.read()
    x = np.frombuffer(raw[:len(raw) // frame * frame], dtype=np.int16)
    if n_ch == 2:
        x = x.reshape(-1, 2).mean(axis=1)
    return x.astype(np.float32) / 32768.0, rate


def judge(x, rate):
    if len(x) < rate // 2:
        return {"verdict": "TOO SHORT"}
    rms = float(np.sqrt((x ** 2).mean()))
    peak = float(np.abs(x).max())
    if rms < 0.003:
        return {"verdict": "SILENCE", "rms": round(rms, 4)}
    clip_pct = float((np.abs(x) > 0.985).mean() * 100)
    N = 8192
    segs = len(x) // N
    flats = []
    tilts = []
    for i in range(min(segs, 24)):
        s = x[i * N:(i + 1) * N] * np.hanning(N)
        psd = np.abs(np.fft.rfft(s)) ** 2 + 1e-12
        # audible band only
        fax = np.fft.rfftfreq(N, 1 / rate)
        m = (fax > 80) & (fax < min(15000, rate * 0.45))
        p = psd[m]
        flats.append(float(np.exp(np.log(p).mean()) / p.mean()))
        lo = p[:len(p) // 4].mean()
        hi = p[3 * len(p) // 4:].mean()
        tilts.append(float(10 * np.log10(lo / (hi + 1e-12) + 1e-12)))
    flat = float(np.median(flats))
    tilt = float(np.median(tilts))
    crest = peak / (rms + 1e-9)
    if clip_pct > 2.0:
        verdict = "CLIPPED"
    elif flat > 0.5 and tilt < 6:
        verdict = "STATIC"
    elif flat < 0.5 or tilt >= 10:
        verdict = "MUSIC/SPEECH"
    else:
        verdict = "UNCERTAIN"
    return {"verdict": verdict, "rms": round(rms, 3),
            "flatness": round(flat, 3), "tilt_db": round(tilt, 1),
            "crest": round(crest, 1), "clip_pct": round(clip_pct, 2)}


if __name__ == "__main__":
    path = sys.argv[1]
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
    x, rate = read_wav_tail(path, secs)
    r = judge(x, rate)
    r["rate"] = rate
    print(r)
