"""drm_hunt.py - DRM day-shift hunter (task #34).

Primary prey: WINB Red Lion PA, DRM tests on 15670 kHz daytime (per drm.org;
EiBi only lists their 9265 analog service). Secondary prey: whatever EiBi
DIGITAL row is on the air this hour and plausibly receivable on the US East
Coast (Radio Romania slots hard-coded below).

Per cycle (every ~45 min, warden-polite, priority 50, skip-don't-stack):
  1. capture 60 s at 2.048 MS/s per candidate on antenna A (AM loop)
  2. release the SDR, resample to 48 kHz, run the cyclic-prefix fingerprint
     (DRM useful-symbol lengths are integer samples at 48 kHz:
      A=1152, B=1024, C=704, D=448) + PSD occupancy check
  3. archive the 48 kHz cf32 to lab/hunt/, verdict line to lab/drm_day_log.md
  4. if DRM-LIKE: immediately grab 5 minutes, bridge via drm_to_wav.py
     conventions (mono 12 kHz IF wav - the validated path) and feed the
     Dream console decoder (Z:\\src\\dream\\console\\dream.exe, GPL -
     credit drm.sourceforge.io / Volker Fischer et al.). "DecOpen" on
     stderr = FAC/SDC sync + audio service opened.

Safety: balloon-hunt window 06:35-08:25 local = NO SDR (skip cycle);
loop has a hard end time and max cycle count; SIGINT to stop.

Usage:
  python drm_hunt.py            # one cycle now
  python drm_hunt.py --loop     # every 45 min until 22:15 UTC (max 16)
"""
import argparse
import datetime as dt
import subprocess
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
FS2 = 48000.0
HUNT_DIR = HERE / "hunt"
LOG = HERE / "drm_day_log.md"
DREAM = Path(r"Z:\src\dream\console\dream.exe")
PYEXE = sys.executable

# always-on target + hour-gated extras (freq_kHz, UTC on, UTC off, label)
WINB = (15670, 0, 24 * 60, "WINB 15670 DRM test (Red Lion PA)")
EXTRAS = [
    (13690, 16 * 60, 17 * 60, "Radio Romania Int 13690 (Fr, SEu beam)"),
    (13750, 17 * 60, 18 * 60, "Radio Romania Int 13750 (En, WEu beam)"),
    (5910, 18 * 60, 18 * 60 + 30, "Radio Romania Int 5910 (It, SEu beam)"),
    (9570, 18 * 60, 19 * 60, "Radio Romania Int 9570 (De, WEu beam)"),
    (15170, 21 * 60, 22 * 60, "Radio Romania Int 15170 (Es, SAm beam)"),
]
BALLOON = ((6, 35), (8, 25))        # local-time SDR keep-out


def log(line):
    stamp = dt.datetime.utcnow().strftime("%H%M")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"- {stamp}z {line}\n")
    print(f"[hunt] {stamp}z {line}", flush=True)


def in_balloon_window(now=None):
    now = now or dt.datetime.now()
    lo = now.replace(hour=BALLOON[0][0], minute=BALLOON[0][1], second=0)
    hi = now.replace(hour=BALLOON[1][0], minute=BALLOON[1][1], second=0)
    return lo <= now <= hi


def targets_now():
    m = dt.datetime.utcnow().hour * 60 + dt.datetime.utcnow().minute
    out = [WINB]
    out += [e for e in EXTRAS if e[1] <= m < e[2]]
    return out


def open_sdr():
    for _ in range(18):
        devs = SoapySDR.Device.enumerate("driver=sdrplay")
        if devs:
            try:
                return SoapySDR.Device(devs[0])
            except RuntimeError:
                pass
        time.sleep(5)
    return None


def capture(sdr, freq_hz, secs):
    sdr.setFrequency(SOAPY_SDR_RX, 0, freq_hz)
    time.sleep(0.4)
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(st)
    time.sleep(0.2)
    n = int(FS * secs)
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
    return buf


def fingerprint(y):
    """48 kHz baseband -> (occupancy dB, {mode: cp ratio}, best mode)."""
    nfft = 8192
    w = np.hanning(nfft)
    segs = [y[i:i + nfft] * w for i in range(0, len(y) - nfft, nfft)]
    psd = np.mean([np.abs(np.fft.fftshift(np.fft.fft(s))) ** 2
                   for s in segs], axis=0)
    fr = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / FS2))
    inb = np.median(psd[(fr > -4500) & (fr < 4500)])
    outb = np.median(psd[(np.abs(fr) > 8000) & (np.abs(fr) < 16000)])
    occ = 10 * np.log10(inb / (outb + 1e-30))
    scores = {}
    for mode, tu in [("A", 1152), ("B", 1024), ("C", 704), ("D", 448)]:
        def corr(lag):
            prod = y[:-lag] * np.conj(y[lag:])
            m = len(prod) // 4096 * 4096
            return np.abs(prod[:m].reshape(-1, 4096).sum(axis=1)).mean()
        scores[mode] = corr(tu) / (corr(tu + 53) + 1e-12)
    best = max(scores, key=scores.get)
    return occ, scores, best


def try_dream(cf32_path, tag):
    """Bridge to 12 kHz IF wav and run the Dream console for 120 s."""
    subprocess.run([PYEXE, str(HERE / "drm_to_wav.py"), str(cf32_path),
                    "--mode", "if12"], check=True)
    wav = cf32_path.with_suffix("")
    wav = wav.parent / (wav.name + "_if12.wav")
    dec = HUNT_DIR / f"{tag}_decoded.wav"
    err = HUNT_DIR / f"{tag}_dream_err.txt"
    with err.open("w") as ef:
        p = subprocess.Popen([str(DREAM), "-f", str(wav),
                              "-w", str(dec)],
                             cwd=str(DREAM.parent),
                             stdout=subprocess.DEVNULL, stderr=ef)
        try:
            p.wait(timeout=120)
        except subprocess.TimeoutExpired:
            p.kill()
    txt = err.read_text(errors="replace")
    sync = "DecOpen" in txt
    audio = dec.exists() and dec.stat().st_size > 100000
    return sync, audio, dec


def cycle():
    if in_balloon_window():
        log("SKIP cycle - balloon-hunt keep-out window (06:35-08:25 local)")
        return
    tgts = targets_now()
    caps = {}
    with radio_lock.Holder("drm_day", "WINB/DRM day hunt (task 34)", 50):
        sdr = open_sdr()
        if sdr is None:
            log("SKIP cycle - SDR never freed after 90 s")
            return
        sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
        sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        try:
            sdr.writeSetting("biasT_ctrl", "false")
            sdr.writeSetting("rfgain_sel", "3")
            sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40)
        except Exception:
            pass
        for khz, _, _, label in tgts:
            caps[khz] = (capture(sdr, khz * 1e3, 60.0), label)
        del sdr

    hot = []
    stamp = dt.datetime.utcnow().strftime("%Y%m%d_%H%M")
    for khz, (buf, label) in caps.items():
        y = resample_poly(buf, 3, 128).astype(np.complex64)
        out = HUNT_DIR / f"drm_hunt_{stamp}_{khz}_48k.cf32"
        y.tofile(out)
        occ, scores, best = fingerprint(y)
        line = " ".join(f"{m}:{v:.2f}" for m, v in scores.items())
        drmlike = scores[best] > 1.5 and occ > 6
        log(f"{khz} kHz ({label}): in-band {occ:+.1f} dB | CP {line} | "
            f"{'** DRM-LIKE **' if drmlike else 'no DRM'}")
        if drmlike:
            hot.append((khz, label))

    for khz, label in hot:
        log(f"{khz} kHz DRM-LIKE -> grabbing 5 minutes for Dream")
        with radio_lock.Holder("drm_day", "DRM catch - 5 min", 50):
            sdr = open_sdr()
            if sdr is None:
                log(f"{khz} kHz: SDR busy for long capture - lost it")
                continue
            sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
            sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
            sdr.setGainMode(SOAPY_SDR_RX, 0, False)
            try:
                sdr.writeSetting("biasT_ctrl", "false")
                sdr.writeSetting("rfgain_sel", "3")
                sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40)
            except Exception:
                pass
            buf = capture(sdr, khz * 1e3, 300.0)
            del sdr
        y = resample_poly(buf, 3, 128).astype(np.complex64)
        tag = f"drm_catch_{stamp}_{khz}"
        cf = HUNT_DIR / f"{tag}_48k.cf32"
        y.tofile(cf)
        sync, audio, dec = try_dream(cf, tag)
        log(f"{khz} kHz Dream verdict: FAC/SDC sync={sync} audio={audio}"
            f"{' -> ' + dec.name if audio else ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=45 * 60)
    ap.add_argument("--max-cycles", type=int, default=16)
    ap.add_argument("--end-utc", default="22:15")
    args = ap.parse_args()

    HUNT_DIR.mkdir(exist_ok=True)
    if not args.loop:
        cycle()
        return
    eh, em = map(int, args.end_utc.split(":"))
    for i in range(args.max_cycles):
        now = dt.datetime.utcnow()
        if (now.hour, now.minute) >= (eh, em):
            log(f"loop done - past {args.end_utc}z")
            break
        try:
            cycle()
        except KeyboardInterrupt:
            log("SIGINT - hunter stopping")
            raise
        except Exception as e:                          # log, don't die
            log(f"cycle ERROR: {e!r}")
        time.sleep(args.interval)
    else:
        log(f"loop done - max cycles ({args.max_cycles}) reached")


if __name__ == "__main__":
    main()
