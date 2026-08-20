"""sid_watch.py -- the always-on SID carrier watch. v1.0 2026-08-20.

Born from sid_detect.py's honest negative: seven days of 30-minute
broadcast-band sweeps cannot see a solar flare -- a whole SID (~10 min)
fits between two samples, and four of five bands sit on their own noise
floor in daylight. The fix is structural, so this is the structure:

  ONE measurement, MINUTE cadence, ALWAYS-ON CARRIERS, background tier.

Every minute it takes two short grabs on the LF/VLF path and logs the
narrowband power of carriers that never sign off:

  grab A  centre  60 kHz:  NAA 24.0 kHz (Navy MSK, the classic SuperSID
                           target), WWVB 60.0 kHz (NIST -- also feeds the
                           pending phase-tracking idea)
  grab B  centre 300 kHz:  the 292.956 kHz NDB we already receive

plus each grab's own noise floor, so every row carries its own control.
A flare shows as a sudden, minutes-fast change in carrier-over-floor on
the DAYLIT path with the floor steady; sid_detect.py (--source csv) is
the analysis end, GOES is the referee.

Discipline (all house laws):
  * radio_lock at priority 20 (background) -- a skipped minute is logged
    as a skipped minute, never fought for. ~12 s hold, well under TTL.
  * capture integrity: a grab that delivers < 80% of its samples, or
    takes > 2.5x wall, is logged VOID, never averaged in (7/31 law:
    samples==wall*fs or the row lies).
  * append-only JSONL; this script NEVER deletes or rewrites its data.
  * bounded failure: MAX_CONSEC_FAIL cycle failures -> exit 3 (the law:
    respawn needs a ceiling, not hope). A busy radio does not count.
  * magnitude-only measurement, so the 250k phase-corruption law does not
    bite (its own conditions: magnitude consumers were always clean).

Run one cycle:      python sid_watch.py --once
Run as the watch:   python sid_watch.py            (until stopped)
Output:             lab/sid_watch.jsonl  (gitignored with the rest of lab/)

Analysis handoff:   python sid_detect.py --source csv --csv <jsonl>
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import radio_lock                                            # noqa: E402

LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
OUT = LAB / "sid_watch.jsonl"

VERSION = "sid_watch 1.0 2026-08-20"
FS = 250_000.0        # rate-ok: narrowband MAGNITUDE power only (the 8/01
#                       250k corruption is a PHASE defect; magnitude
#                       consumers were clean for weeks, per the law's own
#                       conditions). No phase leaves this file.
GRAB_S = 4.0          # 4 s -> 0.25 Hz FFT bins; carriers are pinned to Hz
CARRIER_HZ = 3.0      # integrate the carrier over +-3 Hz
MAX_CONSEC_FAIL = 8   # not-busy failures in a row before giving up

# Each grab: (centre_hz, [(name, carrier_hz), ...]).  Both centres sit so
# every target is well inside +-125 kHz and away from DC (LO leakage).
GRABS = [
    (60_000.0, [("NAA_24k", 24_000.0), ("WWVB_60k", 60_000.0)]),
    (300_000.0, [("NDB_292p956k", 292_956.0)]),
]


def utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f"[sid_watch] {msg}", flush=True)


def open_sdr(antenna, centre_hz, ifgr, rfgain):
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setFrequency(SOAPY_SDR_RX, 0, centre_hz)
    try:
        sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    except Exception:
        pass
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
    except Exception:
        pass
    sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", ifgr)
    try:
        sdr.writeSetting("rfgain_sel", str(rfgain))
    except Exception:
        pass
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    sdr.activateStream(st)
    return sdr, st


def grab(sdr, st, secs):
    """Sample-bounded WITH a wall gate inside the loop (the hd_probe law)."""
    n_want = int(secs * FS)
    buf = np.empty(2 * 65536, np.int16)
    out = np.empty(2 * n_want, np.int16)
    got = 0
    t0 = time.time()
    while got < n_want and time.time() - t0 < secs * 2.5 + 2:
        r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
        if r.ret > 0:
            n = min(r.ret, n_want - got)
            out[2 * got:2 * (got + n)] = buf[:2 * n]
            got += n
        elif r.ret < 0 and r.ret != -1:
            break
    wall = time.time() - t0
    iq = (out[0::2].astype(np.float32)
          + 1j * out[1::2].astype(np.float32))[:got] / 32768.0
    return iq.astype(np.complex64), got, wall


def carrier_power(iq, centre_hz, target_hz):
    """(carrier dB, floor dB, snr dB) from one FFT of the whole grab.

    Floor = median bin power of the grab's own spectrum excluding +-200 Hz
    around every target -- each row carries its own control, so a gain or
    antenna change moves carrier and floor together and cancels in snr.
    """
    n = len(iq)
    if n < 1024:
        return None
    w = np.hanning(n)
    spec = np.abs(np.fft.fftshift(np.fft.fft(iq * w))) ** 2
    freqs = np.fft.fftshift(np.fft.fftfreq(n, 1.0 / FS)) + centre_hz
    sel = np.abs(freqs - target_hz) <= CARRIER_HZ
    if not sel.any():
        return None
    car = float(spec[sel].sum())
    excl = np.zeros(n, bool)
    for _, targets in GRABS:
        for _, hz in targets:
            excl |= np.abs(freqs - hz) <= 200.0
    floor_bins = spec[~excl & (np.abs(freqs - centre_hz) < FS * 0.4)]
    floor = float(np.median(floor_bins)) * sel.sum()   # same bandwidth
    car_db = 10 * np.log10(car + 1e-30)
    floor_db = 10 * np.log10(floor + 1e-30)
    return dict(carrier_db=round(car_db, 2), floor_db=round(floor_db, 2),
                snr_db=round(car_db - floor_db, 2))


def one_cycle(args):
    """One minute's measurement. Returns 'ok' | 'busy' | 'fail'."""
    if not radio_lock.acquire("sid_watch", "SID carrier watch", 20,
                              wait_s=5):
        h = radio_lock.status() or {}
        log(f"radio held by {h.get('owner', '?')} - minute skipped")
        return "busy"
    row = dict(ts=utc().isoformat(timespec="seconds"),
               ver=VERSION, antenna=args.antenna,
               ifgr=args.ifgr, rfgain=str(args.rfgain))
    ok = True
    sdr = st = None
    try:
        for centre, targets in GRABS:
            if sdr is None:
                sdr, st = open_sdr(args.antenna, centre, args.ifgr,
                                   args.rfgain)
            else:
                import SoapySDR
                sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, centre)
                time.sleep(0.15)
            iq, got, wall = grab(sdr, st, GRAB_S)
            want = int(GRAB_S * FS)
            if got < want * 0.8 or wall > GRAB_S * 2.5:
                # capture-integrity law: a starved grab is VOID, not data
                row[f"grab_{centre / 1e3:.0f}k"] = dict(
                    void=True, got=got, want=want, wall=round(wall, 2))
                ok = False
                continue
            row[f"grab_{centre / 1e3:.0f}k"] = dict(
                got=got, wall=round(wall, 2),
                rms=round(float(np.sqrt((np.abs(iq) ** 2).mean())), 6))
            for name, hz in targets:
                m = carrier_power(iq, centre, hz)
                if m:
                    row[name] = m
            radio_lock.heartbeat()
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
        ok = False
    finally:
        try:
            if st is not None:
                sdr.deactivateStream(st)
                sdr.closeStream(st)
            del sdr, st          # real yield: the API session dies with it
        except Exception:
            pass
        radio_lock.release("sid_watch")
    with open(OUT, "a") as f:                    # append-only, always
        f.write(json.dumps(row) + "\n")
    return "ok" if ok else "fail"


def main():
    ap = argparse.ArgumentParser(
        description="always-on SID carrier watch (background tier)")
    ap.add_argument("--antenna", default=os.environ.get("SID_ANTENNA",
                                                        "Antenna C"))
    ap.add_argument("--ifgr", type=float, default=40.0)
    ap.add_argument("--rfgain", default="0",
                    help="rfgain_sel; 0 = MAX gain on the RSPdx")
    ap.add_argument("--period-s", type=float, default=60.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    log(f"{VERSION} antenna={args.antenna} -> {OUT}")
    fails = 0
    while True:
        t0 = time.time()
        res = one_cycle(args)
        if res == "fail":
            fails += 1
            if fails >= MAX_CONSEC_FAIL:
                log(f"{fails} consecutive failures - giving up (exit 3). "
                    f"A wedged radio needs hands, not retries.")
                return 3
        elif res == "ok":
            fails = 0
        if args.once:
            return 0 if res == "ok" else 1
        time.sleep(max(1.0, args.period_s - (time.time() - t0)))


if __name__ == "__main__":
    sys.exit(main())
