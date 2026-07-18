"""simulcast.py - Radio Tuna: simulcast frequency diversity (IMPROVE #4).

Big shortwave broadcasters transmit the SAME program on several
frequencies at once (CNR1, CRI, VOA relays... and WWV, always). Each
frequency rides a different ionospheric path, and different paths fade
independently (measured rho = +0.07 on WWV 10/15 MHz). One wideband
capture grabs two of them at once; demodulate each, align on the
program envelope, and MRC-combine with CARRIER-amplitude weights - the
fade holes in one frequency get filled by the other. No single-
frequency radio can do this.

First-light: WWV 10+15 MHz -> +3.9 dB over the best single frequency.

Two laws baked in (each cost one failed run):
  - align on known structure (program envelope), never raw audio xcorr
    at low SNR - it hallucinates lags and MRC subtracts;
  - MRC weights come from the carrier (the pilot), not audio
    covariance - the known part works far below where statistics do.

Modes:
  selftest - synthesize a 2-frequency simulcast with independent fades
             and a path delay; prove align + combine beat best single.
  pairs    - list same-station frequency pairs on air (EiBi schedule),
             closest spans first. --at HHMM plans a future hour.
  capture  - one wideband IQ grab covering both frequencies.
  ab       - the diversity A/B on a capture (offline, replayable).
  hunt     - pairs -> capture -> ab, with SDR-busy retries and an
             optional --wait-until for scheduled night runs.

Example:  python simulcast.py hunt --match "CNR ?1|Firedrake"
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
LOG = LAB / "simulcast_log.txt"

AUD = 20_000.0          # channelized rate fed to sync_am.detect_both
MAX_SPAN_HZ = 5_000_000.0
FS_LADDER = [250e3, 500e3, 1e6, 2e6, 3e6, 4e6, 5e6, 6e6]


def log(msg):
    line = f"{datetime.now(timezone.utc):%m-%d %H:%M:%SZ}  {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- pairs

def find_pairs(match=None, at_hhmm=None):
    from broadcast_guide import load_eibi
    rows = load_eibi()
    if not rows:
        print("no EiBi cache - run: broadcast_guide.py fetch")
        return []
    cur = at_hhmm if at_hhmm is not None else (
        datetime.now(timezone.utc).hour * 100 + datetime.now(timezone.utc).minute)
    live = {}
    for (f, a, b, station, lang, tgt) in rows:
        on = (a <= cur < b) if a <= b else (cur >= a or cur < b)
        if not on:
            continue
        if match and not re.search(match, station, re.IGNORECASE):
            continue
        live.setdefault(station, set()).add(f)
    pairs = []
    for station, freqs in live.items():
        fl = sorted(freqs)
        for i in range(len(fl)):
            for j in range(i + 1, len(fl)):
                span = (fl[j] - fl[i]) * 1e3
                if 50e3 <= span <= MAX_SPAN_HZ:
                    pairs.append((station, fl[i], fl[j], span))
    pairs.sort(key=lambda p: p[3])
    return pairs


def cmd_pairs(args):
    pairs = find_pairs(args.match, args.at)
    when = f"{args.at:04d}Z" if args.at is not None else "now"
    print(f"simulcast pairs on air ({when}), span <= {MAX_SPAN_HZ/1e6:.0f} MHz:")
    for station, fa, fb, span in pairs[:25]:
        print(f"  {fa:8.0f} + {fb:8.0f} kHz  span {span/1e3:7.0f} kHz   {station}")
    if not pairs:
        print("  (none)")


# -------------------------------------------------------------- capture

def _open_wide(fs, center):
    from hf_knob import _ensure_sdr_dll_path
    _ensure_sdr_dll_path()
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, fs)
    try:
        sdr.setBandwidth(SOAPY_SDR_RX, 0, min(fs * 0.9, 8e6))
    except Exception:
        pass
    try:
        sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna C")
    except Exception:
        pass
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 40)
        sdr.writeSetting("rfgain_sel", "0")
    except Exception:
        pass
    sdr.setFrequency(SOAPY_SDR_RX, 0, center)
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    sdr.activateStream(st)
    return sdr, st


def do_capture(khz_a, khz_b, secs, tag=""):
    span = abs(khz_b - khz_a) * 1e3
    fs = next(f for f in FS_LADDER if f >= span * 1.25 + 100e3)
    center = 0.5 * (khz_a + khz_b) * 1e3
    sdr, st = _open_wide(fs, center)
    import SoapySDR  # noqa: F401
    time.sleep(0.5)
    buf = np.empty(2 * 262144, np.int16)
    for _ in range(8):
        sdr.readStream(st, [buf], 262144, timeoutUs=1_000_000)
    stamp = datetime.now(timezone.utc).strftime("%m%d_%H%MZ")
    name = tag or f"{khz_a:.0f}_{khz_b:.0f}"
    out = LAB / f"simulcast_{name}_{stamp}.cs16"
    n_want = int(secs * fs)
    got = 0
    with open(out, "wb") as f:
        while got < n_want:
            r = sdr.readStream(st, [buf], 262144, timeoutUs=1_000_000)
            if r.ret > 0:
                n = min(r.ret, n_want - got)
                buf[:2 * n].tofile(f)
                got += n
            elif r.ret < 0 and r.ret != -1:
                log(f"stream err {r.ret} at {got/fs:.0f}s")
                break
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    Path(str(out) + ".json").write_text(json.dumps({
        "freq_hz": center, "fs_hz": int(fs), "format": "cs16",
        "secs": got / fs, "khz_a": khz_a, "khz_b": khz_b,
        "utc": datetime.now(timezone.utc).isoformat(),
        "antenna": "Antenna C (discone)"}, indent=1))
    log(f"captured {got/fs:.0f}s @ {fs/1e6:.2f} MS/s -> {out.name} "
        f"({out.stat().st_size/1e6:.0f} MB)")
    return out


def cmd_capture(args):
    do_capture(args.khz_a, args.khz_b, args.secs)


# ------------------------------------------------------------------- ab

def _extract(cap, fs, offset_hz):
    """Chunked mix + decimate fs -> AUD, phase-continuous."""
    from scipy.signal import resample_poly
    from math import gcd
    mm = np.memmap(cap, np.int16, "r")
    n_total = len(mm) // 2
    chunk = int(5 * fs)
    g = gcd(int(AUD), int(fs))
    pieces = []
    for start in range(0, n_total, chunk):
        end = min(start + chunk, n_total)
        raw = mm[2 * start:2 * end].astype(np.float32) / 32768.0
        x = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
        n = start + np.arange(len(x), dtype=np.float64)
        x = x * np.exp(-2j * np.pi * offset_hz / fs * n).astype(np.complex64)
        pieces.append(resample_poly(x, int(AUD) // g, int(fs) // g)
                      .astype(np.complex64))
    return np.concatenate(pieces)


def _pause_floor(a, fs=int(AUD)):
    from scipy.signal import butter, sosfilt
    sos = butter(4, [300, 3400], btype="band", fs=fs, output="sos")
    v = sosfilt(sos, a).astype(np.float32)
    blk = int(0.05 * fs)
    nb = len(v) // blk
    e = (v[:nb * blk].reshape(nb, blk) ** 2).mean(axis=1)
    return 10 * np.log10(np.percentile(e, 85) / max(np.percentile(e, 15), 1e-15))


def diversity_combine(a_sync, a_amp, b_sync, b_amp):
    """Envelope-align b to a, carrier-weight MRC. Returns (div, a_n, b_n,
    diag)."""
    from scipy.signal import resample_poly as _rp
    n = min(len(a_sync), len(b_sync))
    a, b = a_sync[:n].copy(), b_sync[:n].copy()
    env_a = _rp(np.abs(a), 1000, int(AUD)).astype(np.float32)
    env_b = _rp(np.abs(b), 1000, int(AUD)).astype(np.float32)
    env_a -= env_a.mean(); env_b -= env_b.mean()
    span_ms = 500
    xc = np.correlate(env_a[span_ms:-span_ms], env_b, mode="valid")
    lag_ms = int(np.argmax(xc)) - span_ms
    peak_ratio = float(xc.max() / max(np.partition(xc, -2)[-2], 1e-12))
    lag = int(round(abs(lag_ms) * AUD / 1000))

    def _shift(v, s):
        """s > 0: delay v by s samples; s < 0: advance."""
        if s > 0:
            return np.concatenate([np.zeros(s, np.float32), v[:-s]])
        if s < 0:
            return np.concatenate([v[-s:], np.zeros(-s, np.float32)])
        return v

    # correlate() sign conventions are a bug farm - decide the shift
    # direction EMPIRICALLY: whichever aligned copy correlates harder wins.
    seg = slice(int(2 * AUD), int(n - 2 * AUD))
    if lag:
        cand = [_shift(b, lag), _shift(b, -lag)]
        dots = [abs(float(np.dot(a[seg], c[seg]))) for c in cand]
        b = cand[int(np.argmax(dots))]
    if np.dot(a[seg], b[seg]) < 0:
        b = -b
    blk = int(0.25 * AUD)
    nblk = n // blk
    m = nblk * blk

    def blockw(amp):
        L2 = min(len(amp), m)
        w = amp[:L2].astype(np.float64) ** 2
        nb2 = L2 // blk
        w = w[:nb2 * blk].reshape(nb2, blk).mean(axis=1)
        if nb2 < nblk:
            w = np.pad(w, (0, nblk - nb2), mode="edge")
        return np.convolve(w, np.ones(5) / 5, mode="same")

    ga = float(np.median(a_amp)); gb = float(np.median(b_amp))
    a_n = a[:m] / max(ga, 1e-12)
    b_n = b[:m] / max(gb, 1e-12)
    eA = np.repeat(blockw(a_amp) / max(ga, 1e-12) ** 2, blk)[:m]
    eB = np.repeat(blockw(b_amp) / max(gb, 1e-12) ** 2, blk)[:m]
    div = (eA * a_n + eB * b_n) / (eA + eB)
    L = min(len(a_amp), len(b_amp))
    rho = float(np.corrcoef(a_amp[:L], b_amp[:L])[0, 1])
    return div, a_n, b_n, {"lag_ms": lag_ms, "peak_ratio": round(peak_ratio, 2),
                           "rho": round(rho, 3)}


def cmd_ab(args):
    from sync_am import detect_both, write_wav
    meta = json.loads(Path(args.file + ".json").read_text(encoding="utf-8-sig"))
    fs = float(meta["fs_hz"])
    center = float(meta["freq_hz"])
    ka, kb = float(meta["khz_a"]), float(meta["khz_b"])
    log(f"A/B on {Path(args.file).name}: {ka:.0f} + {kb:.0f} kHz")
    xa = _extract(args.file, fs, ka * 1e3 - center)
    xb = _extract(args.file, fs, kb * 1e3 - center)
    _, a_sync, a_amp = detect_both(xa)
    _, b_sync, b_amp = detect_both(xb)
    div, a_n, b_n, diag = diversity_combine(a_sync, a_amp, b_sync, b_amp)
    sa, sb, sd = _pause_floor(a_n), _pause_floor(b_n), _pause_floor(div)
    best = max(sa, sb)
    log(f"  rho {diag['rho']:+.3f}  lag {diag['lag_ms']} ms "
        f"(peak ratio {diag['peak_ratio']})")
    log(f"  pause-floor: A {sa:.1f}  B {sb:.1f}  MRC {sd:.1f} dB  "
        f"-> {sd-best:+.1f} dB vs best single "
        f"{'*** DIVERSITY WIN ***' if sd - best > 1.0 else ''}")
    tagn = f"{ka:.0f}_{kb:.0f}"
    write_wav(LAB / f"simul_{tagn}_a.wav", a_n)
    write_wav(LAB / f"simul_{tagn}_b.wav", b_n)
    write_wav(LAB / f"simul_{tagn}_div.wav", div)
    log(f"  WAVs: lab/simul_{tagn}_{{a,b,div}}.wav")
    return sd - best


# ----------------------------------------------------------------- hunt

def cmd_hunt(args):
    if args.wait_until:
        target = datetime.fromisoformat(args.wait_until.replace("Z", "+00:00"))
        log(f"hunt armed for {target:%Y-%m-%d %H:%M}Z (match: {args.match})")
        while True:
            dt = (target - datetime.now(timezone.utc)).total_seconds()
            if dt <= 0:
                break
            time.sleep(min(dt, 300))
    pairs = find_pairs(args.match)
    if not pairs:
        log(f"hunt: no pairs on air matching '{args.match}'")
        return 1
    # probe: the schedule says on-air, only the antenna says AUDIBLE.
    # Score each narrow pair by the WEAKER of its two carriers.
    cand = [p for p in pairs if p[3] <= 200e3][:10]
    best = None
    if cand:
        try:
            fs_p = 250e3
            sdr, st = _open_wide(fs_p, cand[0][1] * 1e3)
            import SoapySDR
            from SoapySDR import SOAPY_SDR_RX
            buf = np.empty(2 * 65536, np.int16)
            for station, ka, kb, span in cand:
                sdr.setFrequency(SOAPY_SDR_RX, 0, 0.5 * (ka + kb) * 1e3)
                time.sleep(0.25)
                for _ in range(4):
                    sdr.readStream(st, [buf], 65536, timeoutUs=1_000_000)
                got = []
                for _ in range(12):
                    r = sdr.readStream(st, [buf], 65536, timeoutUs=1_000_000)
                    if r.ret > 0:
                        got.append(buf[:2 * r.ret].copy())
                iq = np.concatenate(got)
                x = (iq[0::2].astype(np.float32) + 1j * iq[1::2].astype(np.float32)) / 32768.0
                nfft = 1 << 14
                seg = x[:len(x) // nfft * nfft].reshape(-1, nfft)
                P = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
                db = 10 * np.log10(P + 1e-12)
                fr = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs_p))
                floor = np.median(db)
                offs = 0.5 * span
                snr = []
                for o in (-offs, +offs):
                    i = np.argmin(np.abs(fr - o))
                    snr.append(db[max(0, i - 2):i + 3].max() - floor)
                score = min(snr)
                log(f"  probe {station[:24]:24s} {ka:6.0f}+{kb:6.0f}: "
                    f"carriers {snr[0]:+5.1f}/{snr[1]:+5.1f} dB")
                if best is None or score > best[0]:
                    best = (score, station, ka, kb, span)
            sdr.deactivateStream(st)
            sdr.closeStream(st)
        except Exception as e:
            log(f"  probe failed ({str(e)[:60]}) - falling back to schedule order")
    if best and best[0] > 6.0:
        _, station, ka, kb, span = best
    else:
        if best:
            log(f"  best probed pair only {best[0]:+.1f} dB - taking it anyway")
            _, station, ka, kb, span = best
        else:
            station, ka, kb, span = pairs[0]
    log(f"hunt: {station}  {ka:.0f} + {kb:.0f} kHz (span {span/1e3:.0f} kHz)")
    deadline = time.time() + 20 * 60
    cap = None
    while time.time() < deadline:
        try:
            cap = do_capture(ka, kb, args.secs,
                             tag=re.sub(r"\W+", "", station)[:12])
            break
        except Exception as e:
            log(f"SDR busy ({str(e)[:60]}) - retry in 45s")
            time.sleep(45)
    if cap is None:
        log("hunt: gave up on SDR")
        return 1
    args2 = argparse.Namespace(file=str(cap))
    cmd_ab(args2)
    return 0


# ------------------------------------------------------------- selftest

def cmd_selftest(args):
    from sync_am import detect_both
    print("=" * 60)
    print("simulcast self-test: 2 carriers, independent fades, 8 ms lag")
    print("=" * 60)
    rng = np.random.default_rng(11)
    fs = 500_000.0
    secs = 24
    t = np.arange(int(secs * fs)) / fs
    # program: band-limited noise "speech" with syllabic gaps
    from scipy.signal import butter, sosfilt, resample_poly
    prog = rng.normal(0, 1, int(secs * 8000)).astype(np.float32)
    sos = butter(4, [200, 3000], btype="band", fs=8000, output="sos")
    prog = sosfilt(sos, prog).astype(np.float32)
    gate = (np.sin(2 * np.pi * 0.7 * np.arange(len(prog)) / 8000) > -0.3)
    prog *= gate
    prog = 0.7 * prog / max(np.abs(prog).max(), 1e-9)
    m = resample_poly(prog, int(fs), 8000).astype(np.float32)[:len(t)]
    lagN = int(0.008 * fs)
    m_b = np.concatenate([np.zeros(lagN, np.float32), m[:-lagN]])
    # slow independent fades (channel B deeper)
    fA = 1.0 + 0.4 * np.sin(2 * np.pi * 0.11 * t + 1.0)
    fB = np.clip(1.0 + 0.9 * np.sin(2 * np.pi * 0.07 * t), 0.05, 2.0)
    sig = (fA * (1 + m) * np.exp(2j * np.pi * (-100e3) / fs * np.arange(len(t)))
           + fB * (1 + m_b) * np.exp(2j * np.pi * (+100e3) / fs * np.arange(len(t))))
    sig = sig.astype(np.complex64)
    sig += (rng.normal(0, 0.05, len(t)) + 1j * rng.normal(0, 0.05, len(t))
            ).astype(np.complex64)
    from math import gcd
    g = gcd(int(AUD), int(fs))

    def ext(off):
        n = np.arange(len(sig), dtype=np.float64)
        x = sig * np.exp(-2j * np.pi * off / fs * n).astype(np.complex64)
        return resample_poly(x, int(AUD) // g, int(fs) // g).astype(np.complex64)

    _, a_sync, a_amp = detect_both(ext(-100e3))
    _, b_sync, b_amp = detect_both(ext(+100e3))
    div, a_n, b_n, diag = diversity_combine(a_sync, a_amp, b_sync, b_amp)
    lag_ok = abs(diag["lag_ms"] - (-8)) <= 2
    print(f"  lag found {diag['lag_ms']} ms (true -8)   rho {diag['rho']:+.3f}")
    # ground-truth judge: output SNR vs the KNOWN message over the WHOLE
    # clip - fade holes count. (Pause-floor rewards a channel's best
    # moments and is blind to holes; keep it for live captures only.)
    ref = resample_poly(m, int(AUD), int(fs)).astype(np.float32)

    def ref_snr(x):
        L = min(len(x), len(ref)) - int(2 * AUD)
        xx, rr = x[int(AUD):L], ref[int(AUD):L]
        gn = np.dot(xx, rr) / np.dot(rr, rr)
        err = xx - gn * rr
        return 10 * np.log10(gn ** 2 * np.dot(rr, rr) / max(np.dot(err, err), 1e-15))

    ra, rb, rd = ref_snr(a_n), ref_snr(b_n), ref_snr(div)
    print(f"  vs truth: A {ra:5.1f} dB   B {rb:5.1f} dB   MRC {rd:5.1f} dB "
          f"({rd - max(ra, rb):+.1f} vs best single)")
    ok = lag_ok and rd - max(ra, rb) > 2.0
    print("=" * 60)
    print("SELFTEST", "PASS" if ok else "FAIL")
    print("=" * 60)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    p = sub.add_parser("pairs")
    p.add_argument("--match", default=None)
    p.add_argument("--at", type=int, default=None, help="UTC HHMM, e.g. 0200")
    c = sub.add_parser("capture")
    c.add_argument("--khz-a", type=float, required=True)
    c.add_argument("--khz-b", type=float, required=True)
    c.add_argument("--secs", type=float, default=120)
    b = sub.add_parser("ab")
    b.add_argument("--file", required=True)
    h = sub.add_parser("hunt")
    h.add_argument("--match", default=r"CNR ?1|Firedrake|China National Radio 1")
    h.add_argument("--secs", type=float, default=120)
    h.add_argument("--wait-until", default=None, help="UTC ISO, e.g. 2026-07-19T01:45:00Z")
    args = ap.parse_args()
    fn = {"selftest": cmd_selftest, "pairs": cmd_pairs, "capture": cmd_capture,
          "ab": cmd_ab, "hunt": cmd_hunt}[args.cmd]
    r = fn(args)
    sys.exit(r if isinstance(r, int) else 0)


if __name__ == "__main__":
    main()
