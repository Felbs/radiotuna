"""broadcast_guide.py - Radio Tuna: ONE guide for everything listenable.

The listening-room master screen: every broadcast your antenna can hear,
on one page, with names where we can get them.

  FM  88-108     carrier survey at 2 MS/s (8 channels per hop) + HD/IBOC
                 sideband detection; names/subchannels merge in from the
                 radio_panel survey cache (lab/stations.json) if present.
  AM  530-1700   the am_night scanner: carriers + HD flags.
  SW  broadcast  carriers found on the 5 kHz raster across 6 bands, then
                 JOINED against the EiBi worldwide schedule: frequency +
                 time-of-day -> station name, language, target region.
                 ("what's audible NOW" - the shortwave concierge.)

Modes:
  fetch   - download/cache the EiBi season schedule (net, no radio)
  survey  - live FM + AM + SW sweep -> lab/guide_data.json (radio, ~2 min)
  show    - render the guide (terminal + lab/guide.html) from cached data

Typical: python broadcast_guide.py survey && python broadcast_guide.py show
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from am_night import scan_hop as am_scan_hop, HOPS as AM_HOPS   # noqa: E402
from hf_knob import SWBC, _ensure_sdr_dll_path                  # noqa: E402

_ensure_sdr_dll_path()

LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
DATA = LAB / "guide_data.json"
EIBI = LAB / "eibi_sked.csv"
HTML = LAB / "guide.html"
STATIONS = LAB / "stations.json"        # radio_panel's HD survey cache

EIBI_URLS = [
    "http://www.eibispace.de/dx/sked-a25.csv",
    "http://www.eibispace.de/dx/sked-b25.csv",
    "http://www.eibispace.de/dx/sked-b24.csv",
]


# ==========================================================================
# EiBi schedule
# ==========================================================================
def fetch_eibi(force=False):
    if EIBI.exists() and not force and time.time() - EIBI.stat().st_mtime < 7 * 86400:
        return EIBI
    for url in EIBI_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "radiotuna"})
            data = urllib.request.urlopen(req, timeout=25).read()
            if len(data) > 50_000:
                EIBI.write_bytes(data)
                print(f"[eibi] fetched {url.rsplit('/', 1)[-1]}: {len(data)//1024} kB")
                return EIBI
        except Exception as e:
            print(f"[eibi] {url.rsplit('/', 1)[-1]}: {e}")
    return EIBI if EIBI.exists() else None


def load_eibi():
    """rows: (khz, start_hhmm, end_hhmm, station, lang, target)"""
    if not EIBI.exists():
        return []
    rows = []
    txt = EIBI.read_text(encoding="latin-1", errors="replace")
    for line in txt.splitlines():
        p = line.split(";")
        if len(p) < 6:
            continue
        try:
            khz = float(p[0])
        except ValueError:
            continue
        tr = p[1].replace(" ", "")
        if "-" not in tr:
            continue
        try:
            a, b = tr.split("-")[:2]
            rows.append((khz, int(a), int(b), p[4].strip(), p[5].strip(),
                         p[6].strip() if len(p) > 6 else ""))
        except ValueError:
            continue
    return rows


def on_air_now(rows, khz, tol=2.0):
    now = datetime.now(timezone.utc)
    cur = now.hour * 100 + now.minute
    out = []
    for (f, a, b, station, lang, tgt) in rows:
        if abs(f - khz) > tol:
            continue
        live = (a <= cur < b) if a <= b else (cur >= a or cur < b)
        if live:
            out.append((station, lang, tgt))
    return out


# ==========================================================================
# SDR survey
# ==========================================================================
def open_sdr(fs, antenna):
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, fs)
    try:
        sdr.setAntenna(SOAPY_SDR_RX, 0, antenna)
    except Exception:
        pass
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 35)
        sdr.writeSetting("rfgain_sel", "1")
    except Exception:
        pass
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    sdr.activateStream(st)
    return sdr, st


def grab(sdr, st, secs, fs):
    n_want = int(secs * fs)
    buf = np.empty(2 * 65536, np.int16)
    out = np.empty(2 * n_want, np.int16)
    got = 0
    while got < n_want:
        r = sdr.readStream(st, [buf], 65536, timeoutUs=1_000_000)
        if r.ret > 0:
            n = min(r.ret, n_want - got)
            out[2 * got:2 * (got + n)] = buf[:2 * n]
            got += n
        elif r.ret < 0 and r.ret != -1:
            break
    return ((out[0::2].astype(np.float32) + 1j * out[1::2].astype(np.float32))
            / 32768.0).astype(np.complex64)[:got]


def spec_db(iq, nfft):
    seg = iq[:len(iq) // nfft * nfft].reshape(-1, nfft)
    seg = seg * np.hanning(nfft).astype(np.float32)
    P = (np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2).mean(axis=0)
    return 10 * np.log10(P + 1e-12)


def survey_fm(antenna):
    """88.1-107.9 in 2 MS/s hops (8 channels each): SNR + HD shoulders."""
    from SoapySDR import SOAPY_SDR_RX
    fs = 2_000_000.0
    sdr, st = open_sdr(fs, antenna)
    stations = []
    for center in np.arange(88.9e6, 108.4e6, 1.6e6):
        sdr.setFrequency(SOAPY_SDR_RX, 0, float(center))
        time.sleep(0.12)
        iq = grab(sdr, st, 1.2, fs)
        db = spec_db(iq, 16384)
        n = len(db)
        binw = fs / n
        c = n // 2
        med = float(np.median(db))
        # US FM channels sit on ODD tenths: 88.1, 88.3, ... 107.9
        for m10 in range(881, 1080, 2):
            f = m10 * 1e5
            if abs(f - center) > 0.78e6:
                continue
            mhz = m10 / 10.0
            i = c + int(round((f - center) / binw))
            if not (60 < i < n - 60):
                continue
            snr = float(db[i - 8:i + 9].max() - med)
            if snr < 12:
                continue
            hd = []
            for sgn in (-1, 1):
                a = i + int(sgn * 135e3 / binw)
                b = i + int(sgn * 185e3 / binw)
                lo, hi = min(a, b), max(a, b)
                if 0 < lo and hi < n:
                    hd.append(float(np.mean(db[lo:hi]) - med))
            stations.append({"mhz": mhz, "snr_db": round(snr, 1),
                             "hd": len(hd) == 2 and min(hd) > 8.0})
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    best = {}
    for s in stations:
        if s["mhz"] not in best or s["snr_db"] > best[s["mhz"]]["snr_db"]:
            best[s["mhz"]] = s
    return sorted(best.values(), key=lambda s: s["mhz"])


def survey_am(antenna):
    from SoapySDR import SOAPY_SDR_RX
    fs = 250_000.0
    sdr, st = open_sdr(fs, antenna)
    out = {}
    for hop in AM_HOPS:
        sdr.setFrequency(SOAPY_SDR_RX, 0, hop)
        time.sleep(0.12)
        iq = grab(sdr, st, 2.0, fs)
        for s in am_scan_hop(iq, hop):
            if s["khz"] not in out or s["snr_db"] > out[s["khz"]]["snr_db"]:
                out[s["khz"]] = s
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    return sorted(out.values(), key=lambda s: -s["snr_db"])


def survey_sw(antenna):
    from SoapySDR import SOAPY_SDR_RX
    fs = 250_000.0
    sdr, st = open_sdr(fs, antenna)
    found = []
    for name, f in SWBC:
        sdr.setFrequency(SOAPY_SDR_RX, 0, f)
        time.sleep(0.12)
        iq = grab(sdr, st, 2.5, fs)
        db = spec_db(iq, 8192)
        n = len(db)
        binw = fs / n
        c = n // 2
        med = float(np.median(db))
        for koff in range(-24, 25):
            if koff == 0:
                continue
            i = c + int(round(koff * 5000.0 / binw))
            if 2 < i < n - 3:
                pk = float(db[i - 1:i + 2].max() - med)
                if pk > 8.0:
                    found.append({"khz": round((f + koff * 5000.0) / 1e3),
                                  "snr_db": round(pk, 1), "band": name})
        # DRM hunt: a ~10 kHz OFDM BLOCK raised above the floor with NO
        # dominant carrier spike (flat top) = digital shortwave
        w = int(10e3 / binw)
        step = max(1, w // 4)
        for a in range(4, n - w - 4, step):
            blk = db[a:a + w]
            lift = float(np.mean(blk) - med)
            crest = float(np.max(blk) - np.mean(blk))
            if lift > 6.0 and crest < 6.0:
                khz = round((f + (a + w / 2 - c) * binw) / 1e3)
                found.append({"khz": khz, "snr_db": round(lift, 1),
                              "band": name, "drm": True})
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    best = {}
    for s in found:
        if s["khz"] not in best or s["snr_db"] > best[s["khz"]]["snr_db"]:
            best[s["khz"]] = s
    return sorted(best.values(), key=lambda s: -s["snr_db"])


def cmd_survey(args):
    t0 = time.time()
    print(f"[survey] FM band ...")
    fm = survey_fm(args.antenna)
    print(f"         {len(fm)} FM carriers ({sum(1 for s in fm if s['hd'])} HD)")
    print(f"[survey] AM band ...")
    am = survey_am(args.antenna)
    print(f"         {len(am)} AM carriers ({sum(1 for s in am if s['iboc'])} HD)")
    print(f"[survey] shortwave bands ...")
    sw = survey_sw(args.antenna)
    print(f"         {len(sw)} SW carriers")
    data = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fm": fm, "am": am, "sw": sw}
    DATA.write_text(json.dumps(data, indent=1))
    print(f"[survey] done in {time.time()-t0:.0f}s -> {DATA.name}")


def cmd_show(args):
    if not DATA.exists():
        print("no survey data - run `survey` first (needs the SDR)")
        return
    d = json.loads(DATA.read_text())
    eibi = load_eibi()
    hd_names = {}
    if STATIONS.exists():
        try:
            for s in json.loads(STATIONS.read_text()).get("stations", []):
                hd_names[round(float(s.get("mhz", 0)), 1)] = s
        except Exception:
            pass
    age_min = None
    try:
        ts = datetime.fromisoformat(d["ts"])
        age_min = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
    except Exception:
        pass
    L = []
    L.append("=" * 66)
    L.append(f"  THE BROADCAST GUIDE - everything your antenna hears"
             + (f"   ({age_min} min old)" if age_min is not None else ""))
    L.append("=" * 66)
    L.append("")
    L.append(f"-- FM ({len(d['fm'])} stations) " + "-" * 40)
    for s in d["fm"]:
        info = hd_names.get(s["mhz"], {})
        nm = info.get("name") or ""
        progs = info.get("programs") or []
        sub = f"  [{len(progs)} HD subchannels]" if progs else ""
        hd = " HD" if s["hd"] else "   "
        L.append(f"  {s['mhz']:>6.1f} MHz  +{s['snr_db']:>5.1f} dB{hd}  {nm}{sub}")
    L.append("")
    L.append(f"-- AM ({len(d['am'])} carriers, strongest 15) " + "-" * 26)
    for s in d["am"][:15]:
        hd = " HD(IBOC)" if s.get("iboc") else ""
        L.append(f"  {s['khz']:>5} kHz  +{s['snr_db']:>5.1f} dB{hd}")
    L.append("")
    L.append(f"-- SHORTWAVE ({len(d['sw'])} carriers) + EiBi 'on air now' " + "-" * 12)
    for s in d["sw"][:20]:
        who = on_air_now(eibi, s["khz"])
        tag = "; ".join(f"{st} ({lg}->{tg})" for st, lg, tg in who[:2]) if who \
            else "(no schedule match)"
        drm = " DRM!" if s.get("drm") else ""
        L.append(f"  {s['khz']:>6} kHz  +{s['snr_db']:>5.1f} dB{drm}  {tag}")
    text = "\n".join(L)
    print(text)
    esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    HTML.write_text(
        "<title>Radio Tuna - Broadcast Guide</title>"
        "<style>body{background:#0d1014;color:#c6d0dc;font-family:Consolas,"
        "monospace;font-size:14px;padding:24px}pre{line-height:1.5}"
        "</style><pre>" + esc + "</pre>", encoding="utf-8")
    print(f"\n[show] wrote {HTML}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--force", action="store_true")
    s = sub.add_parser("survey")
    s.add_argument("--antenna", default="Antenna C")
    sub.add_parser("show")
    args = ap.parse_args()
    if args.cmd == "fetch":
        fetch_eibi(force=args.force)
    elif args.cmd == "survey":
        fetch_eibi()
        cmd_survey(args)
    elif args.cmd == "show":
        fetch_eibi()
        cmd_show(args)


if __name__ == "__main__":
    main()
