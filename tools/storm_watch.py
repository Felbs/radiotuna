#!/usr/bin/env python3
"""storm_watch.py - ionoTuna's self-triggering lightning + ionosphere watch.

THE IDEA: lightning is the transmitter we don't own. Every stroke fires a
broadband VLF "sferic" into the Earth-ionosphere waveguide; the far ones
arrive as dispersed "tweeks" whose falling tail rings toward the waveguide's
TM1 cutoff. Fit the group delay t(f) = t0 + D*f/sqrt(f^2 - fc^2) and the
cutoff fc hands you the ionospheric reflection height h = c/(2*fc), while
the dispersion coefficient D hands you the propagation distance d = c*D.
(One antenna = distance, NOT bearing - the panel draws honest range rings.)
The waveguide physics + textbook-tweek quality gates follow the dlayer-diary
lineage: Z:/src/dlayer-diary/tweeks/analyze_tweeks.py (2026-07-20/21 night,
median fc ~1.7 kHz, h in the published 83-90 km night band).

WHAT IT DOES, unattended:
  * every hour (configurable): a 30 s VLF sniff - 2.048 MS/s at 40 kHz
    center on Antenna A (the battery K-180WLA loop: bias-T stays OFF),
    decimated x64/3 to 96 kHz. Impulse events = envelope excursions above
    k*sigma (robust MAD sigma, k=6) in the 2.5-9 kHz sferic band, with a
    3 ms debounce. Logs the rate (impulses/min) and self-learns the quiet
    baseline (median of recent non-triggered sniffs).
  * TRIGGER: rate above ~5x the learned baseline (plus an absolute floor so
    an all-quiet baseline can't trigger on nothing) -> STORM SESSION:
    a 10-minute capture at warden priority 90 (an active storm is an
    unrepeatable event, same rank as a satellite pass), then per-impulse
    spectrograms, dispersed-tweek vs local-click classification, and
    dispersion fits for the strongest tweeks -> distance + ceiling height.
  * results land in lab/iono_state.json (the panel's /api/iono reads it)
    and lab/storm_watch_log.txt.

RADIO ETIQUETTE (the laws):
  * single-tenant SDR via radio_lock ONLY. Sniffs at priority 20
    (background), storm sessions at 90. Skip-don't-stack: if the radio is
    busy past the polite wait, the cycle is SKIPPED and logged, never queued.
  * overflow-count every capture; captures yield mid-stream to any
    higher-priority waiter (a partial capture is analyzed honestly).
  * hard skip 06:30-08:25 local (the balloon window).
  * max-retries everywhere: bounded SDR-open attempts, and 6 consecutive
    failed cycles = the daemon stands down (exits nonzero) instead of
    grinding a broken radio all night.
  * one storm session per 2 h maximum, no matter what the rate says.
  * SIGINT is the one true kill; every exit path releases the radio.

  python tools/storm_watch.py once      # one sniff now (storm if triggered)
  python tools/storm_watch.py run       # foreground hourly loop
  python tools/storm_watch.py status    # pretty-print lab/iono_state.json
  tools/storm_start.ps1                 # detached daemon (never auto-run)

Pure numpy/scipy - no GPU. QTH never appears here or in the data: distances
are relative ranges, heights are physics.
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import radio_lock

LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
STATE_PATH = LAB / "iono_state.json"
LOG_PATH = LAB / "storm_watch_log.txt"

# ---- capture geometry -------------------------------------------------
FS_IN = 2.048e6              # SDR rate (the RSPdx's proven HF/LF rate here)
CENTER = 40e3                # capture center: VLF band sits at -38.5..-30 kHz
UP, DOWN = 3, 64             # 2.048e6 * 3/64 = 96 kHz exactly
FS = 96000.0                 # decimated rate; tweek energy 1.5-10 kHz lives here
SNIFF_SECS = 30.0
STORM_SECS = 600.0
ANTENNA = "Antenna A"        # K-180WLA loop - battery powered, bias-T OFF

# ---- detection --------------------------------------------------------
K_SIGMA = 6.0                # envelope threshold in robust sigmas
DEBOUNCE_S = 0.003           # >= the ~2 ms spec; kills ringing double-counts
DET_BAND = (2500.0, 9000.0)  # sferic detection band (dlayer-diary's choice)

# ---- trigger ----------------------------------------------------------
TRIGGER_FACTOR = 5.0         # x quiet baseline
TRIGGER_FLOOR = 30.0         # impulses/min absolute floor - a dead-quiet
                             # baseline must not trigger on a whisper
MIN_BASELINE_N = 3           # need this many quiet sniffs before arming
BASELINE_KEEP = 48           # rolling history cap
STORM_COOLDOWN_S = 7200.0    # max one storm session per 2 h
SNIFF_INTERVAL_S = float(os.environ.get("STORM_INTERVAL_S", 3600))

# ---- warden -----------------------------------------------------------
OWNER = "storm_watch"
SNIFF_PRI = 20               # background, same rank as the atlas
STORM_PRI = 90               # unrepeatable event (sat passes hold 100)
SNIFF_WAIT_S = 180.0         # polite wait, then skip-don't-stack
STORM_WAIT_S = 120.0
MAX_FAIL_CYCLES = 6          # consecutive failures before standing down

# ---- physics ----------------------------------------------------------
C_KM_MS = 299.792458         # km per ms: distance = C_KM_MS * D_ms
H_GATE = (60.0, 110.0)       # plausible night reflection heights, km
D_GATE = (300.0, 5000.0)     # plausible waveguide-fit distances, km
FC_GATE = (1.35, 2.6)        # kHz; h ~ 58-111 km (dlayer-diary gate)
TOPN_FIT = 120               # strongest candidates to spectrogram + fit
TWEEKS_KEEP = 12             # strongest good fits published to the panel

BLK = 2_097_152              # capture block: 64*32768 samples (~1.02 s)
PAD = 2048                   # overlap-save context (multiple of 64)


def log(msg):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st):
    st["ts_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def in_balloon_window(now=None):
    t = now or time.localtime()
    mins = t.tm_hour * 60 + t.tm_min
    return 6 * 60 + 30 <= mins <= 8 * 60 + 25


# ======================================================================
#  capture: warden-polite, overflow-counted, yield-aware
# ======================================================================
class _Decim:
    """Overlap-save polyphase decimator, sample-exact across blocks.
    Feeds must be multiples of 64; each feed emits len(x)*3/64 samples
    (offset -PAD/2 in input time - contiguous across feeds, both edges
    of every resample_poly call kept PAD/2 away from the output)."""

    def __init__(self):
        from scipy.signal import resample_poly
        self._rp = resample_poly
        self.carry = np.zeros(PAD, np.complex64)

    def feed(self, x):
        buf = np.concatenate([self.carry, x])
        y = self._rp(buf, UP, DOWN)
        head = (PAD // 2) * UP // DOWN
        out = y[head:head + len(x) * UP // DOWN].astype(np.complex64)
        self.carry = buf[-PAD:]
        return out


def _open_sdr():
    """Bounded-retry open. Never restarts SDRplayAPIService on its own:
    a background daemon yanking the service could sabotage a live
    listening session (set STORM_HEAL=1 to allow one restart attempt)."""
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    healed = False
    sdr = None
    for i in range(8):                       # bounded: ~40 s worst case
        devs = SoapySDR.Device.enumerate("driver=sdrplay")
        if devs:
            try:
                sdr = SoapySDR.Device(devs[0])
                break
            except RuntimeError:
                pass
        if (i >= 3 and not devs and not healed
                and os.environ.get("STORM_HEAL") == "1"):
            healed = True
            log("no RSPdx enumerated - one SDRplayAPIService restart (STORM_HEAL=1)")
            import subprocess
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Restart-Service SDRplayAPIService"],
                           capture_output=True, timeout=60)
            time.sleep(8)
        time.sleep(5)
    if sdr is None:
        return None, None, None
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS_IN)
    sdr.setAntenna(SOAPY_SDR_RX, 0, ANTENNA)
    sdr.setFrequency(SOAPY_SDR_RX, 0, CENTER)
    sdr.setGainMode(SOAPY_SDR_RX, 0, True)   # AGC ON - the LW lesson (E4v2)
    try:
        sdr.writeSetting("biasT_ctrl", "false")   # battery loop - NEVER bias
    except Exception:
        pass
    return sdr, SOAPY_SDR_RX, SOAPY_SDR_CF32


def capture_vlf(secs, priority, purpose, wait_s):
    """Grab `secs` of VLF, decimated to 96 kHz complex64.
    Returns (y96, overflows, note) - y96 is None when the radio was
    busy/broken (note says which); a yield mid-capture returns the
    partial buffer with note='yielded: ...' if >= 10 s made it."""
    with radio_lock.Holder(OWNER, purpose, priority, wait_s=wait_s) as h:
        if not h.ok:
            return None, 0, "radio busy - skipped (skip-don't-stack)"
        sdr, RX, CF32 = _open_sdr()
        if sdr is None:
            return None, 0, "SDR never opened (bounded retries exhausted)"
        st = None
        out_chunks, overflows = [], 0
        note = ""
        dec = _Decim()
        dq = queue.Queue(maxsize=24)

        def _worker():
            while True:
                item = dq.get()
                if item is None:
                    return
                out_chunks.append(dec.feed(item))

        wk = threading.Thread(target=_worker, daemon=True)
        wk.start()
        try:
            st = sdr.setupStream(RX, CF32)
            sdr.activateStream(st)
            time.sleep(0.3)
            n_total = int(FS_IN * secs) // BLK * BLK
            chunk = np.empty(131072, np.complex64)
            block = np.empty(BLK, np.complex64)
            fill, got, last_hb = 0, 0, 0.0
            while got < n_total:
                r = sdr.readStream(st, [chunk], len(chunk),
                                   timeoutUs=int(1e6))
                if r.ret > 0:
                    take = min(r.ret, BLK - fill)
                    block[fill:fill + take] = chunk[:take]
                    fill += take
                    if take < r.ret:            # block boundary straddle
                        rem = chunk[take:r.ret].copy()
                    else:
                        rem = None
                    if fill == BLK:
                        try:
                            dq.put_nowait(block.copy())
                        except queue.Full:
                            overflows += 1      # honest: we dropped a block
                        got += BLK
                        fill = 0
                        if rem is not None:
                            block[:len(rem)] = rem
                            fill = len(rem)
                elif r.ret < 0:
                    overflows += 1              # capture-protocol law: COUNT
                now = time.time()
                if now - last_hb > 20:
                    radio_lock.heartbeat()
                    last_hb = now
                    why = radio_lock.should_yield()
                    if why:
                        note = f"yielded: {why}"
                        break
        finally:
            try:
                if st is not None:
                    sdr.deactivateStream(st)
                    sdr.closeStream(st)
            except Exception:
                pass
            # the 7/20 lesson: the Device keeps the hardware claimed
            # until GC - drop and collect before releasing the lock
            sdr = st = None
            import gc
            gc.collect()
            dq.put(None)
            wk.join(timeout=30)
    if not out_chunks:
        return None, overflows, note or "no samples"
    y = np.concatenate(out_chunks)
    if note and len(y) < 10 * FS:
        return None, overflows, note + " (too short to score)"
    return y, overflows, note


# ======================================================================
#  DSP: real VLF, impulse events, tweek dispersion fits
# ======================================================================
def to_real_vlf(y96):
    """Shift the 40 kHz-centered baseband up so 0 Hz is true 0 Hz, take
    the real part -> a real VLF waveform at 96 kS/s (the tweek band
    1.5-10 kHz is in there; 30-38.5 kHz folds on top, but those are
    steady MSK carriers and the spectrogram's per-row median subtraction
    removes steady lines - the analyze_tweeks trick)."""
    # 40000/96000 = 5/12: the phase ramp repeats every 12 samples
    pat = np.exp(1j * 2 * np.pi * (5.0 / 12.0) * np.arange(12))
    reps = int(np.ceil(len(y96) / 12))
    ramp = np.tile(pat, reps)[:len(y96)]
    return (y96 * ramp).real.astype(np.float32)


def detect_events(xr, k=K_SIGMA, debounce_s=DEBOUNCE_S):
    """Envelope excursions above k robust sigmas in the sferic band.
    Returns (event_sample_indices, envelope, threshold)."""
    from scipy.signal import butter, sosfilt
    sos = butter(4, list(DET_BAND), btype="band", fs=FS, output="sos")
    ew = np.abs(sosfilt(sos, xr.astype(np.float64)))
    med = np.median(ew)
    sigma = np.median(np.abs(ew - med)) * 1.4826 + 1e-12
    thr = med + k * sigma
    above = ew > thr
    edges = np.where((~above[:-1]) & (above[1:]))[0]
    gap = int(debounce_s * FS)
    kept = []
    for e in edges:
        if not kept or e - kept[-1] > gap:
            kept.append(int(e))
    return kept, ew, thr


def _tail_scores(xr, events):
    """Rank events by the dispersion signature: a strong, long-ringing
    tail in the 1.75-2.6 kHz near-cutoff band (dlayer-diary's score)."""
    from scipy.signal import butter, sosfilt
    sos_tail = butter(4, [1750, 2600], btype="band", fs=FS, output="sos")
    et = np.abs(sosfilt(sos_tail, xr.astype(np.float64)))
    w_pre = int(0.002 * FS)
    w_tail = int(0.010 * FS)
    out = []
    for e in events:
        if e - 3 * w_pre < 0 or e + w_tail >= len(et):
            continue
        base = np.sqrt(np.mean(et[e - 3 * w_pre:e - w_pre] ** 2)) + 1e-9
        seg = et[e + int(0.003 * FS):e + w_tail]
        tail_rms = np.sqrt(np.mean(seg ** 2))
        tail_ms = float(np.sum(et[e:e + w_tail] > 3 * base) / FS * 1000.0)
        out.append(dict(samp=int(e), score=float(tail_rms / base * tail_ms),
                        tail_ms=tail_ms))
    out.sort(key=lambda d: d["score"], reverse=True)
    return out


def _powerline_frac(events):
    """Honesty diagnostic: fraction of inter-event intervals sitting on
    the 120 Hz power-line comb (n * 8.333 ms +- 0.6 ms). A high value
    says the counted 'sferics' are the HOUSE, not the sky."""
    if len(events) < 10:
        return None
    iei = np.diff(np.asarray(events)) / FS * 1000.0
    ph = iei / (1000.0 / 120.0)
    on_comb = np.abs(ph - np.round(ph)) * (1000.0 / 120.0) < 0.6
    return round(float(np.mean(on_comb)), 2)


def group_delay(f, t0, D, fc):
    """Waveguide group delay t(f) = t0 + D*f/sqrt(f^2-fc^2). f in kHz,
    t in ms. (dlayer-diary lineage.)"""
    f = np.asarray(f, dtype=np.float64)
    out = np.full_like(f, np.nan)
    m = f > fc
    out[m] = t0 + D * f[m] / np.sqrt(f[m] ** 2 - fc ** 2)
    return out


def extract_ridge(x, fmin=1.55, fmax=6.5):
    """Per-frequency arrival time with steady-carrier removal and a
    monotone-descent constraint (adapted from analyze_tweeks to 96 kS/s)."""
    from scipy.signal import stft
    nperseg, nfft, hop = 384, 4096, 8
    f, t, Z = stft(x, fs=FS, nperseg=nperseg, nfft=nfft,
                   noverlap=nperseg - hop, window="hann",
                   boundary=None, padded=False)
    S = np.abs(Z) ** 2
    S = np.maximum(S - np.median(S, axis=1, keepdims=True), 0.0)
    fk = f / 1000.0
    tms = t * 1000.0
    detband = (fk >= 2.5) & (fk <= 7.0)
    onset_j = int(np.argmax(S[detband].sum(axis=0)))
    onset = tms[onset_j]
    twin = (tms >= onset - 1.5) & (tms <= onset + 16.0)
    tb = tms[twin]
    band = (fk >= fmin) & (fk <= fmax)
    fb = fk[band]
    Sb = S[band][:, twin]
    if Sb.size == 0:
        return np.array([]), np.array([]), np.array([])
    smax = Sb.max() + 1e-30
    step = max(1, int(round(0.08 / (fb[1] - fb[0]))))
    rf, rt, rw = [], [], []
    for i in range(0, len(fb), step):
        row = Sb[i, :]
        j = int(np.argmax(row))
        if row[j] < 0.08 * smax:
            continue
        rf.append(fb[i]); rt.append(tb[j]); rw.append(row[j])
    rf, rt, rw = np.array(rf), np.array(rt), np.array(rw)
    if len(rf) > 3:
        o = np.argsort(-rf)
        rf, rt, rw = rf[o], rt[o], rw[o]
        keep = np.zeros(len(rf), bool)
        tprev = -1e9
        for kk in range(len(rf)):
            if rt[kk] >= tprev - 0.6:
                keep[kk] = True
                tprev = max(tprev, rt[kk])
        rf, rt, rw = rf[keep], rt[keep], rw[keep]
    return rf, rt, rw


def _fit_core(rf, rt, rw, curve_fit):
    fc0 = max(1.5, rf.min() - 0.15)
    p0 = [np.min(rt), 1.0, fc0]
    lo = [np.min(rt) - 8, 1e-3, 1.4]
    hi = [np.max(rt) + 8, 1e3, rf.min()]
    if hi[2] <= lo[2]:
        return None
    sigma = 1.0 / np.sqrt(np.maximum(rw, rw.max() * 1e-3))
    try:
        popt, pcov = curve_fit(group_delay, rf, rt, p0=p0, sigma=sigma,
                               bounds=(lo, hi), maxfev=30000)
    except Exception:
        return None
    return popt, np.sqrt(np.diag(pcov))


def fit_one(rf, rt, rw):
    """Iteratively-trimmed dispersion fit (dlayer-diary lineage)."""
    from scipy.optimize import curve_fit
    if len(rf) < 6:
        return None
    order = np.argsort(rf)
    rf, rt, rw = rf[order], rt[order], rw[order]
    keep = np.ones(len(rf), bool)
    for _ in range(4):
        res = _fit_core(rf[keep], rt[keep], rw[keep], curve_fit)
        if res is None:
            return None
        popt, _ = res
        resid = rt - group_delay(rf, *popt)
        rms_all = np.sqrt(np.nanmean(resid[keep] ** 2))
        newkeep = np.abs(resid) < max(2.2 * rms_all, 0.4)
        newkeep &= np.isfinite(resid)
        if newkeep.sum() < 6 or (newkeep == keep).all():
            keep = newkeep if newkeep.sum() >= 6 else keep
            break
        keep = newkeep
    res = _fit_core(rf[keep], rt[keep], rw[keep], curve_fit)
    if res is None:
        return None
    popt, perr = res
    resid = (rt - group_delay(rf, *popt))[keep]
    kf = rf[keep]
    return dict(t0=float(popt[0]), D=float(popt[1]), fc=float(popt[2]),
                fc_err=float(perr[2]),
                rms=float(np.sqrt(np.nanmean(resid ** 2))),
                dspread=float(np.nanmax(rt[keep]) - np.nanmin(rt[keep])),
                npts=int(keep.sum()),
                rmin=float(kf.min()), rmax=float(kf.max()))


def classify_and_fit(xr, t_wall0, max_fits=TOPN_FIT):
    """Storm-session analysis: events -> clicks vs dispersed tweeks;
    fit the strongest tweek candidates. Returns a summary dict."""
    events, ew, thr = detect_events(xr)
    cands = _tail_scores(xr, events)
    win = int(0.045 * FS)
    pre = int(0.006 * FS)
    n_clicks = sum(1 for c in cands if c["tail_ms"] < 1.5)
    fits, n_fit_tried = [], 0
    for c in cands[:max_fits]:
        if c["tail_ms"] < 1.5:
            continue                      # undispersed local click
        s = c["samp"] - pre
        if s < 0 or s + win > len(xr):
            continue
        n_fit_tried += 1
        rf, rt, rw = extract_ridge(xr[s:s + win].astype(np.float64))
        fit = fit_one(rf, rt, rw)
        if fit is None:
            continue
        fc = fit["fc"]
        h_km = C_KM_MS / 2.0 * (1.0 / fc)          # c/(2 fc); fc kHz -> km
        d_km = fit["D"] * C_KM_MS
        # textbook gates (dlayer-diary) + the honest sanity gates
        textbook = (fit["npts"] >= 12 and FC_GATE[0] <= fc <= FC_GATE[1]
                    and fit["rms"] <= 0.45 and fit["fc_err"] <= 0.20
                    and 2.0 <= fit["dspread"] <= 13.0
                    and fit["rmax"] >= 5.0 and fit["rmin"] <= 2.6)
        physical = (H_GATE[0] <= h_km <= H_GATE[1]
                    and D_GATE[0] <= d_km <= D_GATE[1])
        quality = ("good" if textbook and physical else
                   "unphysical" if textbook else "poor")
        fits.append({
            "t_utc": datetime.fromtimestamp(
                t_wall0 + c["samp"] / FS, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "D_km": round(d_km, 0), "h_km": round(h_km, 1),
            "fc_khz": round(fc, 3), "fc_err_khz": round(fit["fc_err"], 3),
            "rms_ms": round(fit["rms"], 3), "npts": fit["npts"],
            "tail_ms": round(c["tail_ms"], 1), "quality": quality})
    good = [f for f in fits if f["quality"] == "good"]
    good.sort(key=lambda f: f["rms_ms"])
    secs = len(xr) / FS
    n_dispersed = sum(1 for c in cands if c["tail_ms"] >= 1.5)
    out = {
        "secs": round(secs, 1),
        "n_events": len(events),
        "rate_per_min": round(len(events) / (secs / 60.0), 1),
        "n_dispersed": n_dispersed,
        "rate_dispersed_per_min": round(n_dispersed / (secs / 60.0), 1),
        "powerline_frac": _powerline_frac(events),
        "n_clicks_local": n_clicks,
        "n_fit_tried": n_fit_tried,
        "n_fits": len(fits),
        "n_tweeks_good": len(good),
        "n_unphysical": sum(1 for f in fits if f["quality"] == "unphysical"),
        "tweeks": good[:TWEEKS_KEEP],
    }
    if good:
        out["h_km_median"] = round(
            float(np.median([f["h_km"] for f in good])), 1)
        out["fc_khz_median"] = round(
            float(np.median([f["fc_khz"] for f in good])), 3)
    return out


# ======================================================================
#  the watch itself
# ======================================================================
def quick_tweek_peek(xr, t_wall0, max_fits=25):
    """A light version for the hourly sniff: try a few fits so the panel's
    ceiling gauge can wake up on any decent night, not just in storms."""
    return classify_and_fit(xr, t_wall0, max_fits=max_fits)


def do_sniff(secs=SNIFF_SECS):
    """One polite sniff. Returns (verdict, rate_or_None)."""
    st = load_state()
    t0_wall = time.time()
    y, overflows, note = capture_vlf(secs, SNIFF_PRI,
                                     "hourly VLF sferics sniff", SNIFF_WAIT_S)
    if y is None:
        log(f"sniff: no capture - {note}")
        st["sniff"] = {"verdict": "NO-RADIO", "note": note,
                       "overflows": overflows}
        save_state(st)
        return "NO-RADIO", None
    xr = to_real_vlf(y)
    del y
    peek = quick_tweek_peek(xr, t0_wall)
    rate = peek["rate_per_min"]

    base = st.get("baseline", {"history": []})
    hist = base.get("history", [])
    baseline = float(np.median(hist)) if hist else None
    armed = len(hist) >= MIN_BASELINE_N
    threshold = (max(TRIGGER_FACTOR * baseline, TRIGGER_FLOOR)
                 if armed else None)
    triggered = bool(armed and rate > threshold)
    elevated = bool(baseline and rate > 2 * baseline)
    verdict = ("STORM-TRIGGER" if triggered else
               "ELEVATED" if elevated else
               "LEARNING" if not armed else "QUIET")
    if not triggered:                    # only quiet air teaches the baseline
        hist.append(rate)
        base["history"] = hist[-BASELINE_KEEP:]
    base["rate_per_min"] = (round(float(np.median(base["history"])), 1)
                            if base.get("history") else None)
    base["n_samples"] = len(base.get("history", []))

    st["sniff"] = {
        "ts_utc": datetime.fromtimestamp(t0_wall, tz=timezone.utc
                                         ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "secs": round(peek["secs"], 1), "rate_per_min": rate,
        "n_events": peek["n_events"],
        "rate_dispersed_per_min": peek["rate_dispersed_per_min"],
        "powerline_frac": peek["powerline_frac"],
        "n_clicks_local": peek["n_clicks_local"],
        "n_tweeks_good": peek["n_tweeks_good"], "overflows": overflows,
        "verdict": verdict, "note": note or None,
    }
    if peek.get("h_km_median") is not None:
        st["sniff"]["h_km_median"] = peek["h_km_median"]
        st["sniff"]["tweeks"] = peek["tweeks"][:4]
    st["baseline"] = base
    st["trigger"] = {"factor": TRIGGER_FACTOR, "floor": TRIGGER_FLOOR,
                     "threshold": (round(threshold, 1) if threshold else None),
                     "armed": armed}
    save_state(st)
    log(f"sniff: {rate:.1f} impulses/min ({peek['n_events']} events/"
        f"{peek['secs']:.0f}s, {peek['n_clicks_local']} local clicks, "
        f"dispersed {peek['rate_dispersed_per_min']}/min, "
        f"powerline-comb {peek['powerline_frac']}, "
        f"{peek['n_tweeks_good']} good tweeks, overflow {overflows}) "
        f"baseline {base['rate_per_min']} -> {verdict}")
    return verdict, rate


def storm_session():
    """The 10-minute priority-90 capture + full tweek analysis."""
    st = load_state()
    last = st.get("storm", {}).get("last_session_epoch", 0)
    if time.time() - last < STORM_COOLDOWN_S:
        log("storm: TRIGGERED but inside the 2 h cooldown - skipping")
        return
    if in_balloon_window():
        log("storm: TRIGGERED but inside the balloon window - skipping")
        return
    log(f"storm: SESSION START - {STORM_SECS:.0f} s at priority {STORM_PRI}")
    t0_wall = time.time()
    y, overflows, note = capture_vlf(STORM_SECS, STORM_PRI,
                                     "TRIGGERED storm session", STORM_WAIT_S)
    if y is None:
        log(f"storm: capture failed - {note}")
        return
    xr = to_real_vlf(y)
    del y
    log(f"storm: captured {len(xr) / FS:.0f} s (overflow {overflows}"
        f"{', ' + note if note else ''}) - analyzing...")
    res = classify_and_fit(xr, t0_wall)
    res["ts_utc"] = datetime.fromtimestamp(
        t0_wall, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    res["last_session_epoch"] = t0_wall
    res["overflows"] = overflows
    if note:
        res["note"] = note
    st = load_state()
    st["storm"] = res
    save_state(st)
    log(f"storm: DONE - {res['n_events']} events, "
        f"{res['n_tweeks_good']} textbook tweeks"
        + (f", median h {res['h_km_median']} km" if res.get("h_km_median")
           else "") + f", {res['n_unphysical']} fits marked unphysical")


def cycle():
    """One watch cycle. Returns True on success (resets the fail counter)."""
    if in_balloon_window():
        log("cycle: balloon window (06:30-08:25) - hard skip")
        st = load_state()
        st["sniff"] = {"verdict": "SKIPPED-BALLOON",
                       "ts_utc": datetime.now(timezone.utc
                                              ).strftime("%Y-%m-%dT%H:%M:%SZ")}
        save_state(st)
        return True
    verdict, rate = do_sniff()
    if verdict == "STORM-TRIGGER":
        storm_session()
    return verdict != "NO-RADIO"


def cmd_run():
    log(f"storm_watch daemon up: sniff every {SNIFF_INTERVAL_S:.0f} s, "
        f"trigger {TRIGGER_FACTOR}x baseline (floor {TRIGGER_FLOOR}/min), "
        f"storm cooldown {STORM_COOLDOWN_S / 3600:.0f} h")
    fails = 0
    try:
        while True:
            t0 = time.time()
            try:
                ok = cycle()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"cycle error: {e!r}")
                ok = False
            fails = 0 if ok else fails + 1
            if fails >= MAX_FAIL_CYCLES:
                log(f"{fails} consecutive failed cycles - standing down "
                    "(the max-retries law). Fix the radio, restart me.")
                sys.exit(1)
            sleep_s = max(60.0, SNIFF_INTERVAL_S - (time.time() - t0))
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        log("SIGINT - storm_watch down clean")


def cmd_status():
    st = load_state()
    if not st:
        print("no lab/iono_state.json yet - run `storm_watch.py once`")
        return
    print(json.dumps(st, indent=2))


def main():
    ap = argparse.ArgumentParser(description="ionoTuna storm watch")
    ap.add_argument("mode", choices=["run", "once", "status"])
    args = ap.parse_args()
    if args.mode == "run":
        cmd_run()
    elif args.mode == "once":
        try:
            cycle()
        except KeyboardInterrupt:
            log("SIGINT during once - clean exit")
    else:
        cmd_status()


if __name__ == "__main__":
    main()
