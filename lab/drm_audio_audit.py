#!/usr/bin/env python3
"""Honest audit of a Dream-decoded DRM audio file.

Dream writes its -w wav with RIFF/data length fields left at zero when it is
stopped at end-of-file, so most players see an empty file.  This repairs the
header, then answers the only question that matters: is this real programme
audio, or is the decoder emitting plausible-looking mush?

Tests, in order of how hard they are to fake:
  * duration / RMS / peak / share of active frames
  * band energy + 95% spectral rolloff (a 12 kHz AAC service cannot have
    energy above 6 kHz; speech rolls off around 3 kHz)
  * envelope modulation spectrum - speech puts most of its envelope energy in
    the 2-8 Hz syllabic band
  * pause depth - speech has real silence between phrases

Usage:  python drm_audio_audit.py catch.wav [more.wav ...]
        python drm_audio_audit.py catch.wav --snippet out20s.wav

Written 2026-07-29 for the radiotuna DRM campaign (task #34).
"""
import sys
import wave
import numpy as np


def load(path):
    """Read a wav, repairing Dream's zero-length RIFF/data fields in memory."""
    d = bytearray(open(path, 'rb').read())
    if d[:4] != b'RIFF' or d[8:12] != b'WAVE':
        raise ValueError('not a RIFF/WAVE file')
    import struct
    i = 12
    while i + 8 <= len(d):
        cid, sz = bytes(d[i:i + 4]), struct.unpack('<I', d[i + 4:i + 8])[0]
        if cid == b'data':
            if sz == 0 or i + 8 + sz > len(d):
                struct.pack_into('<I', d, i + 4, len(d) - (i + 8))
                struct.pack_into('<I', d, 4, len(d) - 8)
            break
        i += 8 + sz + (sz & 1)
    else:
        raise ValueError('no data chunk')
    import io
    w = wave.open(io.BytesIO(bytes(d)), 'rb')
    nch, fs, n = w.getnchannels(), w.getframerate(), w.getnframes()
    x = np.frombuffer(w.readframes(n), dtype='<i2').astype(np.float64)
    w.close()
    return x, nch, fs


def audit(path, snippet=None):
    x, nch, fs = load(path)
    mono = x.reshape(-1, nch).mean(axis=1) if nch > 1 else x
    dur = len(mono) / fs
    rms = float(np.sqrt(np.mean(mono ** 2))) if len(mono) else 0.0
    peak = float(np.abs(mono).max()) if len(mono) else 0.0
    print(f"\n{path}\n  {fs} Hz, {nch} ch, {dur:.1f} s, peak {peak:.0f}, RMS {rms:.1f}")
    if rms < 1:
        print("  -> SILENT, nothing decoded")
        return

    fl = max(1, int(0.05 * fs))
    fr = mono[:len(mono) // fl * fl].reshape(-1, fl)
    frms = np.sqrt((fr ** 2).mean(axis=1))
    print(f"  active frames {(frms > 0.02 * peak).mean() * 100:.0f}%")

    # spectrum of the loudest 10 s
    seg = min(int(10 * fs), len(mono))
    starts = range(0, max(1, len(mono) - seg), seg)
    st = max(starts, key=lambda i: np.sum(mono[i:i + seg] ** 2))
    y = mono[st:st + seg]
    y = (y - y.mean()) * np.hanning(len(y))
    S = np.abs(np.fft.rfft(y)) ** 2
    f = np.fft.rfftfreq(len(y), 1 / fs)
    tot = S.sum()
    edges = [0, 300, 1000, 3000, 6000, 12000, fs / 2]
    print("  band energy: " + " | ".join(
        f"{a:.0f}-{b:.0f}Hz {100 * S[(f >= a) & (f < b)].sum() / tot:4.1f}%"
        for a, b in zip(edges[:-1], edges[1:])))
    roll = float(f[np.searchsorted(np.cumsum(S) / tot, 0.95)])
    print(f"  spectral centroid {(f * S).sum() / tot:.0f} Hz, 95% rolloff {roll:.0f} Hz")

    # envelope modulation
    dec = max(1, fs // 200)
    env = np.abs(mono[:len(mono) // dec * dec].reshape(-1, dec)).mean(axis=1)
    efs = fs / dec
    E = np.abs(np.fft.rfft((env - env.mean()) * np.hanning(len(env)))) ** 2
    ef = np.fft.rfftfreq(len(env), 1 / efs)
    band = lambda a, b: E[(ef >= a) & (ef < b)].sum()
    t = band(0.2, 40) or 1.0
    syl = band(2, 8) / t
    q5, q50, q95 = np.percentile(env, [5, 50, 95])
    pause = q5 / max(q95, 1e-9)
    print(f"  envelope: 0.2-2Hz {100*band(0.2,2)/t:.0f}% | 2-8Hz(syllabic) {100*syl:.0f}%"
          f" | 8-20Hz {100*band(8,20)/t:.0f}%   pause/peak {pause:.3f}")
    print("  verdict: " + ("SPEECH-LIKE (syllabic modulation + real pauses)"
                           if syl > 0.25 and pause < 0.35 else
                           "tonal / continuous - not obviously speech"))

    if snippet:
        s = int(20 * fs)
        starts = range(0, max(1, len(mono) - s), s)
        st = max(starts, key=lambda i: np.sum(mono[i:i + s] ** 2))
        o = wave.open(snippet, 'wb')
        o.setnchannels(nch); o.setsampwidth(2); o.setframerate(fs)
        o.writeframes(x[st * nch:(st + s) * nch].astype('<i2').tobytes())
        o.close()
        print(f"  snippet: {snippet} (20 s from t={st / fs:.0f}s)")


if __name__ == '__main__':
    args = sys.argv[1:]
    snippet = None
    if '--snippet' in args:
        i = args.index('--snippet')
        snippet = args[i + 1]
        args = args[:i] + args[i + 2:]
    if not args:
        print(__doc__)
        raise SystemExit(1)
    for p in args:
        try:
            audit(p, snippet)
        except Exception as e:
            print(f"{p}: UNREADABLE ({e})")
