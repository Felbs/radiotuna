"""am_listen.py - Radio Tuna AM deck: LIVE medium-wave listening.

Continuous loop: capture a chunk, synchronous-AM demodulate (carrier-locked
PLL - the FPLL idea on AM, borrowed from sw_listen), append to a growing raw
s16 file that mpv tails. Offset-tuned so the SDR's DC spike never sits on the
carrier the PLL locks to (the 7/26 AM-HD lesson).

  python am_listen.py --khz 820 --play
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hf_knob import open_sdr, grab, FS          # 250 kS/s HF/MW front end
from sw_listen import synchronous_am            # carrier-locked detector

LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
MPV = (os.environ.get("MPV_EXE") or shutil.which("mpv")
       or r"C:\Program Files\MPV Player\mpv.exe")
OFFSET = 30e3            # tune 30 kHz below target; DC stays out of channel
AUD = 24_000             # output audio rate (plenty for 6 kHz AM)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--khz", type=float, required=True)
    ap.add_argument("--antenna", default=os.environ.get("RT_AM_ANTENNA",
                                                        "Antenna A"))
    ap.add_argument("--play", action="store_true")
    ap.add_argument("--chunk", type=float, default=6.0)
    args = ap.parse_args()

    from scipy.signal import resample_poly, butter, sosfilt
    target = args.khz * 1e3
    raw_path = LAB / "am_live.s16"
    raw_path.write_bytes(b"")

    sdr, st = open_sdr(args.antenna)
    import SoapySDR
    sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, target - OFFSET)
    print(f"[am_listen] {args.khz:.0f} kHz on {args.antenna} "
          f"(offset-tuned, sync-AM)", flush=True)

    player = None
    if args.play:
        player = subprocess.Popen(
            [MPV, "--demuxer=rawaudio", "--demuxer-rawaudio-rate=%d" % AUD,
             "--demuxer-rawaudio-channels=1", "--demuxer-rawaudio-format=s16le",
             "--force-seekable=no", "--cache=yes", "--volume=95",
             f"--title=RADIO TUNA AM - {args.khz:.0f} kHz", str(raw_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # slow AGC state that survives across chunks
    agc = 0.1
    sos = None
    try:
        while True:
            iq = grab(sdr, st, args.chunk)
            n = np.arange(len(iq), dtype=np.float64)
            x = iq * np.exp(-2j * np.pi * OFFSET / FS * n)   # target -> DC
            # channel filter +-6 kHz then down to 25 kS/s
            x = resample_poly(x, 1, 10).astype(np.complex64)  # 250k -> 25k
            x = x - np.mean(x)
            audio = synchronous_am(x, 25_000)
            # voice-band shape 100 Hz - 5.5 kHz
            if sos is None:
                sos = butter(4, [100 / 12_500, 5_500 / 12_500],
                             btype="band", output="sos")
            audio = sosfilt(sos, audio).astype(np.float32)
            audio = resample_poly(audio, AUD, 25_000).astype(np.float32)
            # slow AGC riding the fades
            rms = float(np.sqrt(np.mean(audio ** 2)) + 1e-9)
            agc = 0.9 * agc + 0.1 * rms
            audio = np.clip(audio * (0.25 / max(agc, 1e-6)), -1, 1)
            with open(raw_path, "ab") as f:
                f.write((audio * 32000).astype(np.int16).tobytes())
            if player is not None and player.poll() is not None:
                break                            # listener closed the window
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sdr.deactivateStream(st); sdr.closeStream(st)
        except Exception:
            pass
        if player is not None and player.poll() is None:
            player.terminate()


if __name__ == "__main__":
    main()
