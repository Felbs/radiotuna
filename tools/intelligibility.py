"""intelligibility.py - Radio Tuna: the ear that understands words.

The DragonOS/Whisper borrow, aimed at the metric the user actually
asked for: is this station LISTENABLE? faster-whisper (4090) turns
demodulated audio into words with per-segment confidence - so the
quality dial can finally say "I understood 41 words of English" instead
of inferring listenability from SNR physics.

Three outputs per clip:
  INTELLIGIBILITY 0-100  duration-weighted word confidence, hallucination
                         -guarded (Whisper invents text on pure noise;
                         no_speech_prob and compression ratio veto it)
  LANGUAGE               tagged + probability (cross-checks EiBi: is the
                         scheduled Mandarin service really in Mandarin?)
  STATION-ID candidates  spoken callsigns / "this is ..." phrases

  python intelligibility.py FILE.wav
  python intelligibility.py FILE.s16 --s16-rate 24000
"""
import argparse
import re
import sys
import wave
from pathlib import Path

import numpy as np

_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        try:
            _MODEL = WhisperModel("small", device="cuda",
                                  compute_type="float16")
        except Exception:
            _MODEL = WhisperModel("small", device="cpu",
                                  compute_type="int8")
    return _MODEL


def load_audio(path, s16_rate=24_000):
    """Any of our audio artifacts -> float32 mono 16 kHz."""
    from scipy.signal import resample_poly
    from math import gcd
    p = Path(path)
    if p.suffix == ".s16":
        a = np.fromfile(p, np.int16).astype(np.float32) / 32768
        fs = s16_rate
    else:
        w = wave.open(str(p))
        fs = w.getframerate()
        raw = np.frombuffer(w.readframes(w.getnframes()), np.int16)
        a = raw.astype(np.float32) / 32768
        if w.getnchannels() == 2:
            a = a.reshape(-1, 2).mean(axis=1)
    g = gcd(16_000, int(fs))
    return resample_poly(a, 16_000 // g, int(fs) // g).astype(np.float32)


ID_PAT = re.compile(
    r"\b(this is|you're listening to|you are listening to)\s+([A-Z][\w\s\.]{2,40})"
    r"|\b([WK][A-Z]{2,3})\b", re.IGNORECASE)


def analyze(audio_16k):
    """-> dict(score, language, lang_prob, words, wpm, text, ids)."""
    segs, info = _model().transcribe(
        audio_16k, beam_size=3, vad_filter=True,
        condition_on_previous_text=False, word_timestamps=False)
    total_dur = len(audio_16k) / 16_000
    text_parts, weighted, w_dur, n_words = [], 0.0, 0.0, 0
    for s in segs:
        dur = max(s.end - s.start, 0.1)
        conf = float(np.exp(s.avg_logprob))          # ~0.8 clean, ~0.3 junk
        # hallucination veto: Whisper writes fiction over noise
        if s.no_speech_prob > 0.6 or s.compression_ratio > 2.4:
            conf *= 0.15
        weighted += conf * dur
        w_dur += dur
        n_words += len(s.text.split())
        text_parts.append(s.text.strip())
    text = " ".join(text_parts)
    speech_frac = min(1.0, w_dur / max(total_dur, 0.1))
    conf_mean = (weighted / w_dur) if w_dur else 0.0
    # score: how much of the clip was confidently-understood speech
    score = int(np.clip(100 * conf_mean * (0.35 + 0.65 * speech_frac), 0, 100))
    if n_words < 3:
        score = min(score, 10)
    ids = []
    for m in ID_PAT.finditer(text):
        ids.append((m.group(2) or m.group(3) or "").strip())
    return {"score": score, "language": info.language,
            "lang_prob": round(float(info.language_probability), 2),
            "words": n_words,
            "wpm": round(n_words / max(total_dur / 60, 0.01)),
            "speech_frac": round(speech_frac, 2),
            "text": text, "ids": [i for i in ids if i][:5]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--s16-rate", type=int, default=24_000)
    args = ap.parse_args()
    a = load_audio(args.path, args.s16_rate)
    r = analyze(a)
    print(f"INTELLIGIBILITY {r['score']}/100 | {r['language']} "
          f"(p={r['lang_prob']}) | {r['words']} words ({r['wpm']} wpm), "
          f"speech {r['speech_frac']*100:.0f}% of clip")
    if r["ids"]:
        print("STATION-ID candidates:", "; ".join(r["ids"]))
    print("TRANSCRIPT:", (r["text"][:400] + "…") if len(r["text"]) > 400
          else r["text"] or "(nothing intelligible)")


if __name__ == "__main__":
    main()
