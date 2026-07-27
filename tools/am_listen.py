"""am_listen.py - Radio Tuna: LIVE band listening (medium wave AND
shortwave - one loop for the whole HF world).

Producer/consumer: a capture thread streams the SDR continuously into a
queue (no samples lost while DSP runs), the main loop runs the am_best
chain per chunk and feeds audio to mpv over a PIPE. Piping is the law
learned on FM: a player tailing a growing file pauses forever the
moment it catches the live edge - "the sound just stopped."

Every stage (opening SDR, tuning, first capture, audio flowing) is
published to lab/band_quality.json so the panel can show a loading bar
that says exactly what it is doing.

  python am_listen.py --khz 820 --play              # medium wave
  python am_listen.py --khz 13845 --deck sw --play  # shortwave, same loop
"""
import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hf_knob import open_sdr, grab, FS          # 250 kS/s HF/MW front end
import am_best                                  # the best-chain demodulator

LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
MPV = (os.environ.get("MPV_EXE") or shutil.which("mpv")
       or r"C:\Program Files\MPV Player\mpv.exe")
OFFSET = 30e3            # tune 30 kHz below target; DC stays out of channel
AUD = 24_000             # output audio rate (plenty for 6 kHz AM)
SPEC_BINS = 256          # waterfall row width (must divide the 2048 FFT)
QUAL = LAB / "band_quality.json"


def publish(deck, khz, **extra):
    d = {"deck": deck, "khz": khz, "ts": time.time()}
    d.update(extra)
    try:
        QUAL.write_text(json.dumps(d))
    except OSError:
        pass


def chan_spectrum(x, lo_pct=10.0):
    """One waterfall row: dB spectrum of the channelized signal, scaled
    0..255 against its own floor so the panel can palette it."""
    n = 2048
    if len(x) < n:
        return []
    seg = x[:len(x) // n * n].reshape(-1, n) * np.hanning(n).astype(np.float32)
    P = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
    P = 10 * np.log10(P + 1e-15)
    P = P.reshape(SPEC_BINS, -1).mean(axis=1)
    lo = np.percentile(P, lo_pct)
    return [int(v) for v in np.clip((P - lo) * 5.0, 0, 255)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--khz", type=float, required=True)
    ap.add_argument("--deck", default="am", choices=("am", "sw"))
    ap.add_argument("--antenna", default=None)
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--chunk", type=float, default=6.0)
    args = ap.parse_args()
    if args.antenna is None:
        env = "RT_SW_ANTENNA" if args.deck == "sw" else "RT_AM_ANTENNA"
        args.antenna = os.environ.get(env, "Antenna A")

    from scipy.signal import resample_poly
    target = args.khz * 1e3
    raw_path = LAB / "band_live.s16"     # kept for the speaker cast
    raw_path.write_bytes(b"")

    publish(args.deck, args.khz, stage="opening the SDR…")
    sdr, st = open_sdr(args.antenna)
    import SoapySDR
    publish(args.deck, args.khz,
            stage=f"tuning {args.khz:.0f} kHz on {args.antenna}…")
    sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, target - OFFSET)
    print(f"[band_listen] {args.khz:.0f} kHz ({args.deck}) on "
          f"{args.antenna} (offset-tuned, best chain)", flush=True)

    # capture thread: the SDR never waits for the DSP
    iq_q = queue.Queue(maxsize=4)
    stop_ev = threading.Event()

    def capture():
        while not stop_ev.is_set():
            try:
                iq_q.put(grab(sdr, st, args.chunk), timeout=2)
            except queue.Full:
                try:                     # DSP wedged - drop oldest, go on
                    iq_q.get_nowait()
                except queue.Empty:
                    pass
            except Exception:
                return
    threading.Thread(target=capture, daemon=True).start()
    publish(args.deck, args.khz,
            stage=f"capturing first {args.chunk:.0f}s of radio…")

    player = None

    def spawn_player():
        return subprocess.Popen(
            [MPV, "--demuxer=rawaudio", "--demuxer-rawaudio-rate=%d" % AUD,
             "--demuxer-rawaudio-channels=1", "--demuxer-rawaudio-format=s16le",
             "--cache=yes", "--volume=95",
             f"--title=RADIO TUNA {args.deck.upper()} - {args.khz:.0f} kHz",
             "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    state = {}
    chunks_done = 0
    # live Whisper: a background thread transcribes the rolling last
    # ~18 s on the GPU; results ride the next truth-dial publish
    from collections import deque
    aud_ring = deque(maxlen=3)
    whisper_out = {}
    whisper_busy = threading.Event()

    def transcribe_ring():
        try:
            import intelligibility as intel
            from scipy.signal import resample_poly as rp
            a = np.concatenate(list(aud_ring))
            a16 = rp(a, 2, 3).astype(np.float32)     # 24k -> 16k
            r = intel.analyze(a16)
            whisper_out.update(
                intell=r["score"], lang=r["language"],
                lang_p=r["lang_prob"],
                transcript=r["text"][-220:],
                spoken_ids=r["ids"])
        except Exception:
            pass
        finally:
            whisper_busy.clear()

    try:
        while True:
            iq = iq_q.get()
            n = np.arange(len(iq), dtype=np.float64)
            x = iq * np.exp(-2j * np.pi * OFFSET / FS * n)   # target -> DC
            # channelize 250k -> 20k (am_best's native rate)
            x = resample_poly(x, 2, 25).astype(np.complex64)
            x = x - np.mean(x)
            spec = chan_spectrum(x)
            audio, diag = am_best.best_chunk(x, 20_000, state=state)
            audio = resample_poly(audio, AUD, 20_000).astype(np.float32)
            pcm = (np.clip(audio, -1, 1) * 32000).astype(np.int16).tobytes()
            with open(raw_path, "ab") as f:
                f.write(pcm)             # the cast tails this file
            aud_ring.append(np.clip(audio, -1, 1).astype(np.float32))
            if len(aud_ring) == 3 and not whisper_busy.is_set():
                whisper_busy.set()
                threading.Thread(target=transcribe_ring,
                                 daemon=True).start()
            if whisper_out:
                diag.update(whisper_out)
            chunks_done += 1
            if args.play and player is None:
                player = spawn_player()  # first chunk primes the pipe
            if player is not None:
                try:
                    player.stdin.write(pcm)
                    player.stdin.flush()
                except (BrokenPipeError, OSError):
                    break                # listener closed the window
            # the truth dial + waterfall row, for the panel to read
            diag.update({"deck": args.deck, "khz": args.khz,
                         "ts": time.time(), "spec": spec})
            try:
                QUAL.write_text(json.dumps(diag))
            except OSError:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        stop_ev.set()
        try:
            sdr.deactivateStream(st); sdr.closeStream(st)
        except Exception:
            pass
        if player is not None and player.poll() is None:
            try:
                player.stdin.close()
            except OSError:
                pass
            player.terminate()


if __name__ == "__main__":
    main()
