"""radio_panel.py â€” Radio Tuna's listening room.  http://localhost:8643

ALBACORE TUNA RADIO: big frequency readout, a tuning dial with every
station the band survey found, HD subchannel buttons (the "grid"), live
now-playing metadata, MER/BER meters, and a STATS FOR NERDS panel that
streams the live knobs (FM pilot SNR / audio SNR / stereo blend / AGC,
HD decoder identity, day-lab status). HD decodes through the albacore
build (ALBACORE=1); analog FM through fm_stereo.py v2.

  SURVEY â€” two stages: wideband FFT sweep finds carriers (~10 s), then
           nrsc5 probes the strong ones for HD (name, slogan, programs).
           Results cached to lab/stations.json (the radio guide).
  LISTEN â€” click a subchannel: SDR pump -> nrsc5 -> audio PIPE -> mpv
  (tee to a per-session WAV for meters/cast; a player tailing a growing
  file stutters at the live edge = ear-static while the file meters clean),
           stats and metadata streaming to the panel.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audio_probe import read_wav_tail, judge   # the audio liveness dial
import fm_stereo                               # the v2 analog chain

HERE = Path(__file__).resolve().parent
LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
PY = sys.executable          # run helpers with the same python (radioconda)
import os as _os
import shutil as _sh
# HD decoder: prefer the albacore build (certified 1.77x audio with
# ALBACORE=1) over stock nrsc5; NRSC5_EXE env still wins.
ALBACORE_EXE = Path(r"Z:\src\albacore\build\src\nrsc5.exe")
NRSC5 = (_os.environ.get("NRSC5_EXE")
         or (str(ALBACORE_EXE) if ALBACORE_EXE.exists() else None)
         or _sh.which("nrsc5") or r"C:\Tools\nrsc5\nrsc5.exe")
DECODER_TAG = ("albacore ALBACORE=1" if "albacore" in NRSC5.lower()
               else "stock nrsc5")


def _nrsc5_env():
    e = dict(_os.environ)
    if "albacore" in NRSC5.lower():
        e["PATH"] = r"C:\msys64\mingw64\bin;" + e["PATH"]
        e.setdefault("ALBACORE", "1")
        # COSTAS_BW=auto deliberately NOT set: the 7/19 field ledger had
        # auto trail plain ALBACORE=1 in 3/3 cliff A/Bs (8v16, 0v1,
        # 6v10 audio-s) — a regression by the no-regression law.
    return e
MPV = (_os.environ.get("MPV_EXE") or _sh.which("mpv")
       or r"C:\Program Files\MPV Player\mpv.exe")
# the live pipe-attached player (HD path) — module-global so the cast
# endpoints can detach/reattach the local speakers to the audio pipe
PLAYER = {"mpv": None}
MPV_PIPE_ARGS = ["-", "--volume=100", "--cache=yes", "--cache-secs=2",
                 "--force-window=no", "--demuxer=rawaudio",
                 "--demuxer-rawaudio-rate=44100",
                 "--demuxer-rawaudio-channels=2",
                 "--demuxer-rawaudio-format=s16le"]
STATIONS = LAB / "stations.json"
PORT = 8643
FS_NRSC5 = 1_488_375.0
FS_CAP = 2 * FS_NRSC5

STATE = {"mhz": None, "prog": None, "name": None, "listening": False,
         "title": None, "artist": None, "mer_lo": None, "mer_hi": None,
         "ber": None, "sync": False, "stage": "", "pct": 0,
         "audio": None,
         # stats-for-nerds: the knobs, live
         "decoder": None, "pilot_snr_db": None, "audio_snr_db": None,
         "stereo_blend": None, "fm_mode": None, "agc_db": None,
         "antenna": None, "ifgr": None, "rfgain": None,
         "album": None, "genre": None, "message": None, "tower": None,
         "alert": None}

# ── live spectrum + waterfall ────────────────────────────────────────
# The listen paths stash raw cs16 chunks here (throttled, copy only);
# a separate worker does the FFT so the SDR hot loops never pay for it
# (the don't-hammer-the-chain law). ±250 kHz around the dial: the
# station, its HD sidebands, and the neighbors.
import collections as _collections
from datetime import datetime
SPEC = {"pend": None, "last": 0.0,
        "rows": _collections.deque(maxlen=60)}
SPEC_SPAN = 250e3


def spec_feed(chunk):
    now = time.time()
    if now - SPEC["last"] < 0.15:
        return
    SPEC["last"] = now
    SPEC["pend"] = chunk.copy()


def _spec_worker():
    N = 2048
    win = np.hanning(N).astype(np.float32)
    while True:
        time.sleep(0.12)
        c = SPEC["pend"]
        if c is None:
            continue
        SPEC["pend"] = None
        try:
            x = c[0::2].astype(np.float32) + 1j * c[1::2].astype(np.float32)
            nseg = min(6, len(x) // N)
            if nseg < 1:
                continue
            p = (np.abs(np.fft.fft(x[:nseg * N].reshape(-1, N) * win,
                                   axis=1)) ** 2).mean(0)
            db = 10 * np.log10(np.fft.fftshift(p) + 1e-6)
            half = int(SPEC_SPAN / (FS_CAP / N))
            mid = N // 2
            SPEC["rows"].append(
                {"t": round(time.time(), 3),
                 "db": np.round(db[mid - half:mid + half], 1).tolist()})
        except Exception:
            pass


threading.Thread(target=_spec_worker, daemon=True).start()

# ── idle full-band waterfall ─────────────────────────────────────────
# When nobody is listening, sweep 88-108 MHz (4 hops @ 8 MS/s, fixed
# gain so the stitch is seamless) and feed the same waterfall. Lowest
# radio priority of anything in the house (10 < warden 20 < labs 50 <
# human 80 < pass 100): yields via should_yield() between hops, stands
# off satellite pass windows, and stops the instant a listen starts.
BAND = {"rows": _collections.deque(maxlen=45), "hold": False}
BAND_LO, BAND_HI, BAND_BINS = 88.0e6, 108.0e6, 1000


def _band_sweeper():
    import radio_lock
    hops = [90.5e6, 96.0e6, 101.5e6, 107.0e6]
    RATE, N = 8e6, 8192
    win = np.hanning(N).astype(np.float32)
    while True:
        time.sleep(1.0)
        if STATE.get("listening") or BAND["hold"]:
            continue
        try:
            wst = json.loads(Path(
                r"Z:\src\wxTuna\lab\wxsat_status.json").read_text())
            rec, los = wst.get("rec_start"), wst.get("next_los")
            if wst.get("state") == "recording":
                continue
            if rec and los:
                t0 = datetime.fromisoformat(
                    rec.replace("Z", "+00:00")).timestamp()
                t1 = datetime.fromisoformat(
                    los.replace("Z", "+00:00")).timestamp()
                if t0 - 120 <= time.time() <= t1 + 120:
                    continue
        except Exception:
            pass
        if not radio_lock.acquire("panel_idle", "idle band waterfall",
                                  10, wait_s=0):
            time.sleep(4)
            continue
        sdr = st_ = None
        try:
            _ensure_sdr_dll_path()
            import SoapySDR
            from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
            SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
            sdr = SoapySDR.Device("driver=sdrplay")
            sdr.setSampleRate(SOAPY_SDR_RX, 0, RATE)
            sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna C")
            sdr.setGainMode(SOAPY_SDR_RX, 0, False)
            try:
                sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 45)
            except Exception:
                pass
            st_ = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
            sdr.activateStream(st_)
            buf = np.empty(2 * 65536, np.int16)
            while not STATE.get("listening") and not BAND["hold"] \
                    and not radio_lock.should_yield():
                row = np.full(BAND_BINS, np.nan, np.float32)
                for hop in hops:
                    if STATE.get("listening") or BAND["hold"] \
                            or radio_lock.should_yield():
                        break
                    sdr.setFrequency(SOAPY_SDR_RX, 0, hop)
                    for _ in range(5):                    # retune settle
                        sdr.readStream(st_, [buf], 65536, timeoutUs=300000)
                    acc = None
                    for _ in range(6):
                        r = sdr.readStream(st_, [buf], 65536,
                                           timeoutUs=300000)
                        if r.ret != 65536:
                            continue
                        x = (buf[0:2 * N:2].astype(np.float32)
                             + 1j * buf[1:2 * N:2].astype(np.float32))
                        p = np.abs(np.fft.fft(x * win)) ** 2
                        acc = p if acc is None else acc + p
                    if acc is None:
                        continue
                    db = 10 * np.log10(np.fft.fftshift(acc) + 1e-6)
                    fax = np.fft.fftshift(
                        np.fft.fftfreq(N, 1 / RATE)) + hop
                    use = np.abs(fax - hop) < 3.4e6
                    bi = ((fax[use] - BAND_LO) / (BAND_HI - BAND_LO)
                          * BAND_BINS).astype(int)
                    ok = (bi >= 0) & (bi < BAND_BINS)
                    np.fmax.at(row, bi[ok], db[use][ok].astype(np.float32))
                if np.isfinite(row).any():
                    fill = float(np.nanmin(row))
                    r2 = np.where(np.isfinite(row), row, fill)
                    BAND["rows"].append(
                        {"t": round(time.time(), 3),
                         "db": np.round(r2, 1).tolist()})
                radio_lock.heartbeat()
        except Exception:
            time.sleep(5)
        finally:
            try:
                if sdr is not None:
                    sdr.deactivateStream(st_)
                    sdr.closeStream(st_)
            except Exception:
                pass
            # closeStream is NOT enough: the Device object keeps the
            # SDRplay hardware claimed until GC, so a yielding sweeper
            # left the radio busy-with-no-lock and starved the labs
            # (7/20 errand-labs launch). Drop and collect NOW.
            sdr = st_ = None
            import gc
            gc.collect()
            time.sleep(0.5)
            radio_lock.release("panel_idle")


threading.Thread(target=_band_sweeper, daemon=True).start()

FM_KEYS = ("pilot_snr_db", "audio_snr_db", "stereo_blend", "fm_mode",
           "agc_db")


def set_stage(pct, msg):
    STATE.update({"pct": pct, "stage": msg})


def audio_watch(my_gen, wav, on_static=None):
    """The apparatus, embedded: every 10 s judge the WAV tail. Two
    consecutive STATIC verdicts = the sound is a lie; call on_static."""
    bad = 0
    while GEN[0] == my_gen:
        time.sleep(10)
        if GEN[0] != my_gen:
            return
        try:
            x, rate = read_wav_tail(wav, 3.0)
            v = judge(x, rate)
            STATE["audio"] = v.get("verdict")
        except Exception:
            continue
        if v.get("verdict") == "STATIC":
            bad += 1
            if bad >= 2 and on_static and GEN[0] == my_gen:
                on_static()
                return
        else:
            bad = 0
SURVEY = {"running": False, "line": "", "pct": 0}
GEN = [0]
LOCK = threading.Lock()
LIVE_PROCS = []


def _ensure_sdr_dll_path():
    """Bare (non-activated) python can't find the SoapySDR driver DLLs;
    without this every open fails and the panel cries RADIO UNAVAILABLE
    even with the radio sitting idle (bit us 2026-07-18)."""
    if _os.name != "nt":
        return
    root = Path(sys.executable).resolve().parent
    for p in (root / "Library" / "bin",
              Path(r"C:\Program Files\SDRplay\API\x64"),
              Path(r"C:\Program Files\SDRplay\API")):
        if p.is_dir():
            _os.environ["PATH"] = str(p) + _os.pathsep + _os.environ["PATH"]
            try:
                _os.add_dll_directory(str(p))
            except Exception:
                pass


ANT_NICK = {"Antenna A": "rabbit", "Antenna B": "old faithful",
            "Antenna C": "discone"}


def pick_antenna(mhz, mode):
    """The perfect-tune table (fitted from the 3-antenna day-lab cube,
    2026-07-19): per-station winning antenna for 'hd' or 'fm'. The
    antennas are complementary — no single one covers the band (88.5 +
    103.5 only decode on the TV yagi; 93.3 only on rabbit ears) — AND
    the winner map is hour-dependent (the yagi owned midday, the
    discone swept the evening), so consult the hour band first."""
    try:
        t = json.loads((LAB / "radio_tune_table.json").read_text())
        h = time.gmtime().tm_hour
        band = "day" if 11 <= h < 19 else "evening"
        key = f"{mhz:.1f}"
        for tbl in (t.get("by_hour", {}).get(band, {}).get("stations", {}),
                    t["stations"]):
            ent = tbl.get(key, {})
            ant = ent.get(f"{mode}_ant") or ent.get("hd_ant") \
                or ent.get("fm_ant")
            if ant:
                return ant
    except Exception:
        pass
    return "Antenna A"


def open_sdr(mhz, ifgr=59.0, rfgain="3", rate=FS_CAP, ant="Antenna A"):
    _ensure_sdr_dll_path()
    import radio_lock
    if not radio_lock.acquire("panel", f"listen {mhz:.1f}", 80,
                              wait_s=6.0):
        holder = radio_lock.status() or {}
        raise RuntimeError(
            f"radio held by {holder.get('owner', '?')} "
            f"({holder.get('purpose', '?')})")
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    try:
        sdr = SoapySDR.Device("driver=sdrplay")
    except Exception:
        # release the reservation we just took or the failed open
        # deadlocks every retry against OUR OWN lock (bit us 7/19)
        radio_lock.release("panel")
        raise
    sdr.setSampleRate(SOAPY_SDR_RX, 0, rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, mhz * 1e6)
    sdr.setAntenna(SOAPY_SDR_RX, 0, ant)
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
    # DUD-BURNER (law: the first session after a driver-service restart
    # often opens fine but streams ZEROS - the user hears static on a
    # perfectly strong station). Probe a short burst; if it's silence,
    # burn this session and reopen once.
    try:
        probe = np.empty(2 * 65536, np.int16)
        got = 0
        pk = 0
        t0 = time.time()
        while got < 4 * 65536 and time.time() - t0 < 2.0:
            r = sdr.readStream(st, [probe], 65536, timeoutUs=500000)
            if r.ret > 0:
                got += r.ret
                pk = max(pk, int(np.abs(probe[:2 * r.ret]).max()))
        if got == 0 or pk < 20:      # zeros or near-zeros = dud session
            close_sdr(sdr, st)
            time.sleep(0.5)
            sdr = SoapySDR.Device("driver=sdrplay")
            sdr.setSampleRate(SOAPY_SDR_RX, 0, rate)
            sdr.setFrequency(SOAPY_SDR_RX, 0, mhz * 1e6)
            sdr.setAntenna(SOAPY_SDR_RX, 0, ant)
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
    except Exception:
        pass
    return sdr, st


def heal_sdr_service():
    """The SDRplay API wedges after rapid open/close storms (a 28-
    station scan is ~30 cycles). A service restart clears it — do it
    automatically instead of telling the human the radio is haunted."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Restart-Service SDRplayAPIService -Force"],
                   capture_output=True, timeout=90)
    time.sleep(6)
    # burn the post-restart dud session (law: the first session after
    # a service restart streams deaf; the lab burns one, so do we)
    try:
        _ensure_sdr_dll_path()
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
        sdr = SoapySDR.Device("driver=sdrplay")
        sdr.setSampleRate(SOAPY_SDR_RX, 0, FS_CAP)
        sdr.setFrequency(SOAPY_SDR_RX, 0, 93.3e6)
        st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
        sdr.activateStream(st)
        buf = np.empty(2 * 65536, np.int16)
        t0 = time.time()
        while time.time() - t0 < 2.5:
            sdr.readStream(st, [buf], 65536, timeoutUs=300000)
        sdr.deactivateStream(st)
        sdr.closeStream(st)
        time.sleep(1)
    except Exception:
        pass


def close_sdr(sdr, st):
    try:
        sdr.deactivateStream(st)
        sdr.closeStream(st)
    except Exception:
        pass
    try:
        import radio_lock
        radio_lock.release("panel")
    except Exception:
        pass


def cs16_to_cu8(raw_i16):
    return ((raw_i16.astype(np.int32) >> 8) + 128).clip(0, 255).astype(np.uint8)


def decimate2(raw):
    i = raw[0::2].astype(np.int32)
    q = raw[1::2].astype(np.int32)
    i2 = ((i[0::2] + i[1::2]) // 2).astype(np.int16)
    q2 = ((q[0::2] + q[1::2]) // 2).astype(np.int16)
    out = np.empty(2 * len(i2), np.int16)
    out[0::2] = i2
    out[1::2] = q2
    return out


def stop_listen():
    with LOCK:
        GEN[0] += 1
        if STATE.get("mhz"):
            STATE["last_mhz"] = STATE["mhz"]   # sticky band cursor
        STATE.update({"listening": False, "mhz": None, "prog": None,
                      "name": None, "sync": False, "stage": "", "pct": 0,
                      "decoder": None, "antenna": None, "ifgr": None,
                      "rfgain": None, "album": None, "genre": None,
                      "message": None, "tower": None, "alert": None})
        STATE.update({k: None for k in FM_KEYS})
    PLAYER["mpv"] = None
    for p in LIVE_PROCS:
        try:
            p.terminate()
        except Exception:
            pass
    LIVE_PROCS.clear()
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "nrsc5.exe"],
                   capture_output=True)


# ── AM / SW decks (Radio Tuna unification, 2026-07-26) ────────────────────
# One radio, three decks: the albacore FM room plus a medium-wave deck and
# a world-band shortwave deck, all sharing the single-tenant RSPdx through
# the same stop_listen()/LIVE_PROCS discipline.
# NOTE: BAND is the FM sweeper's dict (rows/hold, defined above) — the
# deck state MERGES into it; replacing the dict silently killed the FM
# spectrum thread once (KeyError: 'rows' in a daemon = invisible death)
BAND.update({"am_stations": [], "sw_stations": [], "sw_band": "31m",
             "am_hd": None, "deck": None, "khz": None, "scanning": False})
for _deck_file, _key in (("am_stations.json", "am_stations"),
                         ("sw_stations.json", "sw_stations")):
    try:                       # survive panel restarts with full grids
        BAND[_key] = json.loads((LAB / _deck_file).read_text())
    except (OSError, ValueError):
        pass
AM_ANT = os.environ.get("RT_AM_ANTENNA", "Antenna A")   # the K-180WLA loop
SW_ANT = os.environ.get("RT_SW_ANTENNA", "Antenna A")   # loop covers HF too
SW_BANDS = {"49m": (5850, 6250), "41m": (7200, 7500), "31m": (9350, 9950),
            "25m": (11550, 12150), "22m": (13550, 13900),
            "19m": (15050, 15850), "16m": (17450, 18000)}

# AM ident: NO station list ships in this code. am_db.py scrapes the
# FCC's public AM Query into lab/am_db.json on first use — every user's
# panel fills itself from their own scans + their own fetched copy.
# Set RT_QTH="lat,lon" in the environment (privately) for distance-aware
# ranking; without it, ranking is by licensed power.

_EIBI_FULL = None      # (khz, a, b, station, lang, tgt, itu, site)

# ITU code -> transmitter country (the common world-band senders)
ITU_MAP = {"USA": "USA", "CUB": "Cuba", "CHN": "China", "IND": "India",
           "G": "UK", "F": "France", "D": "Germany", "ROU": "Romania",
           "TUR": "Turkey", "KOR": "S.Korea", "KRE": "N.Korea",
           "J": "Japan", "B": "Brazil", "MRA": "N.Marianas",
           "PHL": "Philippines", "THA": "Thailand", "AUT": "Austria",
           "E": "Spain", "EGY": "Egypt", "IRN": "Iran", "ARS": "Saudi",
           "ALB": "Albania", "BOT": "Botswana", "STP": "São Tomé",
           "MDG": "Madagascar", "UZB": "Uzbekistan", "TJK": "Tajikistan",
           "GUM": "Guam", "ASC": "Ascension Is.", "CAN": "Canada",
           "MEX": "Mexico", "NZL": "New Zealand", "AUS": "Australia",
           "CLN": "Sri Lanka", "SNG": "Singapore", "TWN": "Taiwan",
           "VTN": "Vietnam", "NIG": "Nigeria", "NOR": "Norway"}


def _eibi_full():
    """Full EiBi rows incl. transmitter ITU country + site remark —
    the columns broadcast_guide's lean loader drops."""
    global _EIBI_FULL
    if _EIBI_FULL is not None:
        return _EIBI_FULL
    rows = []
    try:
        import broadcast_guide as bg
        bg.fetch_eibi()
        txt = bg.EIBI.read_text(encoding="latin-1", errors="replace")
        for line in txt.splitlines():
            p = line.split(";")
            if len(p) < 7:
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
                             p[6].strip(), p[3].strip(),
                             p[7].strip() if len(p) > 7 else ""))
            except ValueError:
                continue
    except Exception:
        pass
    _EIBI_FULL = rows
    return rows


def _ident_am(khz):
    """(name, call, tower-map link) from the user's own scraped FCC db."""
    try:
        import am_db
        label, call, link, _n = am_db.lookup(khz)
        return label, call, link
    except Exception:
        return "", "", ""


def _ident_sw(khz):
    """(now-playing label, tx country, short-wave.info link) via EiBi."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    cur = now.hour * 100 + now.minute
    best = None
    any_row = None
    for (f, a, b, st, lang, tgt, itu, site) in _eibi_full():
        if abs(f - khz) > 2.0:
            continue
        any_row = any_row or (st, itu, site)
        live = (a <= cur < b) if a <= b else (cur >= a or cur < b)
        if live and best is None:
            best = (st, lang, tgt, itu, site)
    link = f"https://short-wave.info/index.php?freq={khz:g}"
    if best:
        st, lang, tgt, itu, site = best
        tx = ITU_MAP.get(itu, itu)
        if site:
            tx += f" ({site[:18]})"
        return f"{st} [{lang}→{tgt}]", tx, link
    if any_row:
        st, itu, site = any_row
        return f"({st} — off-air now)", ITU_MAP.get(itu, itu), link
    return "", "", link


def sw_schedule(khz):
    """The 'radio guide': every EiBi entry for this frequency — when to
    listen, who it is, where the transmitter is."""
    out = []
    for (f, a, b, st, lang, tgt, itu, site) in _eibi_full():
        if abs(f - khz) > 2.0:
            continue
        out.append({"time": f"{a:04d}-{b:04d} UTC", "station": st,
                    "lang": lang, "target": tgt,
                    "tx": ITU_MAP.get(itu, itu) + (f" · {site}" if site else "")})
    out.sort(key=lambda r: r["time"])
    return out


def _snapshot_scan(center_hz, fs, secs=2.5, antenna=None, step_hz=10e3,
                   lo_hz=None, hi_hz=None, thresh_db=12.0):
    """One wideband grab -> carriers on a channel raster (dB over floor).
    The 7/26 AM-first-light scanner, generalized."""
    import SoapySDR
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    # single-tenant SDR: a just-stopped listener may still hold the device
    # for a few seconds — retry instead of silently returning 0 stations
    d = None
    for attempt in range(4):
        try:
            d = SoapySDR.Device("driver=sdrplay")
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2.5)
    d.setSampleRate(SoapySDR.SOAPY_SDR_RX, 0, fs)
    d.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, center_hz)
    try:
        d.setAntenna(SoapySDR.SOAPY_SDR_RX, 0, antenna or AM_ANT)
    except Exception:
        pass
    try:
        d.setGain(SoapySDR.SOAPY_SDR_RX, 0, "IFGR", 30)
    except Exception:
        pass
    st = d.setupStream(SoapySDR.SOAPY_SDR_RX, "CF32")
    d.activateStream(st)
    buf = np.zeros(262144, np.complex64)
    ch = []
    t0 = time.time()
    while time.time() - t0 < secs:
        r = d.readStream(st, [buf], len(buf), timeoutUs=800000)
        if r.ret > 0:
            ch.append(buf[:r.ret].copy())
    d.deactivateStream(st)
    d.closeStream(st)
    x = np.concatenate(ch) if ch else np.zeros(4096, np.complex64)
    n = min(len(x), 1 << 21)
    S = np.abs(np.fft.fftshift(np.fft.fft(x[:n] * np.hanning(n)))) ** 2
    P = 10 * np.log10(S + 1e-20)
    f = np.fft.fftshift(np.fft.fftfreq(n, 1 / fs)) + center_hz
    floor = float(np.median(P))
    out = []
    lo = lo_hz if lo_hz else center_hz - fs * 0.45
    hi = hi_hz if hi_hz else center_hz + fs * 0.45
    k = lo - (lo % step_hz)
    while k <= hi:
        m = (f >= k - 2e3) & (f < k + 2e3)
        if m.any():
            db = float(P[m].max() - floor)
            if db > thresh_db:
                out.append({"khz": round(k / 1e3, 1), "db": round(db, 1)})
        k += step_hz
    out.sort(key=lambda s: -s["db"])
    # publish a spectrum row so the deck waterfall paints during scans
    try:
        band_m = (f >= lo) & (f <= hi)
        Pb = P[band_m]
        row = Pb[:len(Pb) // 256 * 256].reshape(256, -1).max(axis=1)
        BAND["scan_spec"] = {
            "row": [int(v) for v in np.clip((row - floor) * 4.0, 0, 255)],
            "lo": lo / 1e3, "hi": hi / 1e3, "ts": time.time()}
    except Exception:
        pass
    return out


def _dx_log(deck, band, stations):
    """Append every scan to the DX logbook — after enough days the
    hour-curves say when each band opens (the three-antenna-day move)."""
    try:
        ts = int(time.time())
        with open(LAB / "dx_log.csv", "a", encoding="utf-8") as f:
            for s in stations:
                ident = (s.get("id") or "").replace(",", ";")
                f.write(f"{ts},{deck},{band},{s['khz']:.0f},"
                        f"{s['db']:.1f},{ident}\n")
    except OSError:
        pass


def dx_summary():
    """Logbook -> band-openings-by-hour + best-ever catches."""
    hours = {}                       # (deck, band) -> {hour: [counts]}
    best = {}                        # khz -> (db, id, deck)
    try:
        with open(LAB / "dx_log.csv", encoding="utf-8") as f:
            rows = f.read().splitlines()
    except OSError:
        return {"hours": {}, "best": []}
    scans = {}                       # (ts, deck, band) -> count
    for line in rows:
        p = line.split(",", 5)
        if len(p) < 6:
            continue
        ts, deck, band, khz, db, ident = p
        try:
            ts, khz, db = int(ts), float(khz), float(db)
        except ValueError:
            continue
        scans.setdefault((ts, deck, band), 0)
        scans[(ts, deck, band)] += 1
        if khz not in best or db > best[khz][0]:
            best[khz] = (db, ident, deck)
    for (ts, deck, band), n in scans.items():
        h = time.localtime(ts).tm_hour
        hours.setdefault(f"{deck}:{band}", {}).setdefault(h, []).append(n)
    curves = {k: {str(h): round(sum(v) / len(v), 1)
                  for h, v in hs.items()}
              for k, hs in hours.items()}
    top = sorted(((db, khz, ident, deck)
                  for khz, (db, ident, deck) in best.items() if ident),
                 reverse=True)[:12]
    return {"hours": curves,
            "best": [{"khz": k, "db": d, "id": i, "deck": dk}
                     for d, k, i, dk in top]}


def _scan_begin(eta_s):
    """Take the radio for a scan: refuse if a scan is already running,
    yield any live listener (single-tenant SDR), start the progress
    clock. Returns False if busy."""
    with LOCK:
        if BAND.get("scanning"):
            return False                # two scan buttons at once — no glitch
        BAND["scanning"] = True
    BAND["hold"] = True                 # bench the FM idle sweeper — two
    if LIVE_PROCS:                      # threads on one sdrplay = native crash
        stop_listen()                   # scans take priority over listening
    time.sleep(2.5)                     # let sweeper/listener fully release
    BAND["scan_t0"] = time.time()
    BAND["scan_eta"] = eta_s
    return True


def am_scan():
    if not _scan_begin(22):
        return
    try:
        found = _snapshot_scan(
            1115e3, 2e6, antenna=AM_ANT, lo_hz=530e3, hi_hz=1700e3)
        if BAND.get("scan_spec"):
            BAND["scan_spec"]["deck"] = "am"
        for s in found:
            s["id"], s["call"], s["link"] = _ident_am(s["khz"])
        BAND["am_stations"] = found
        (LAB / "am_stations.json").write_text(json.dumps(found))
        _dx_log("am", "MW", found)
    finally:
        BAND["scanning"] = False
        if not LIVE_PROCS:
            BAND["hold"] = False


def sw_scan(band):
    if not _scan_begin(25):
        return
    try:
        lo, hi = SW_BANDS.get(band, SW_BANDS["31m"])
        center = (lo + hi) / 2 * 1e3
        span = (hi - lo) * 1e3 + 100e3
        fs = max(1e6, min(8e6, span * 1.25))
        found = _snapshot_scan(
            center, fs, antenna=SW_ANT, step_hz=5e3,
            lo_hz=lo * 1e3, hi_hz=hi * 1e3, thresh_db=10.0)
        if BAND.get("scan_spec"):
            BAND["scan_spec"]["deck"] = "sw"
        for s in found:
            s["id"], s["tx"], s["link"] = _ident_sw(s["khz"])
            s["band"] = band
        BAND["sw_stations"] = found
        BAND["sw_band"] = band
        (LAB / "sw_stations.json").write_text(json.dumps(found))
        _dx_log("sw", band, found)
    finally:
        BAND["scanning"] = False
        if not LIVE_PROCS:
            BAND["hold"] = False


def sw_scan_all():
    """World tour: sweep every broadcast band 49m..16m in one pass,
    accumulating results so the grid fills band by band as it goes."""
    if not _scan_begin(30 * len(SW_BANDS)):
        return
    BAND["sw_stations"] = []
    try:
        acc = []
        for band in SW_BANDS:
            BAND["sw_band"] = band          # progress shows in the chips
            lo, hi = SW_BANDS[band]
            center = (lo + hi) / 2 * 1e3
            span = (hi - lo) * 1e3 + 100e3
            fs = max(1e6, min(8e6, span * 1.25))
            try:
                found = _snapshot_scan(
                    center, fs, antenna=SW_ANT, step_hz=5e3,
                    lo_hz=lo * 1e3, hi_hz=hi * 1e3, thresh_db=10.0)
            except Exception:
                continue                    # a dead band never kills the tour
            if BAND.get("scan_spec"):
                BAND["scan_spec"]["deck"] = "sw"
            for s in found:
                s["id"], s["tx"], s["link"] = _ident_sw(s["khz"])
                s["band"] = band
            acc.extend(found)
            _dx_log("sw", band, found)
            BAND["sw_stations"] = sorted(acc, key=lambda s: -s["db"])
        (LAB / "sw_stations.json").write_text(json.dumps(BAND["sw_stations"]))
    finally:
        BAND["scanning"] = False
        if not LIVE_PROCS:
            BAND["hold"] = False


def band_listen(deck, khz):
    stop_listen()
    BAND["hold"] = True      # bench the FM idle sweeper for the whole
    time.sleep(1)            # session — it shares the one sdrplay device
    with LOCK:
        BAND["deck"] = deck
        BAND["khz"] = khz
        STATE["stage"] = f"{deck.upper()} {khz:g} kHz"
    # one LIVE loop for both decks: continuous best-chain audio +
    # truth dial + waterfall rows (sw_listen's 30 s batch is retired
    # from the panel - 30 silent seconds read as "it never played")
    ant = AM_ANT if deck == "am" else SW_ANT
    try:                     # fresh stage line the instant the button lands
        (LAB / "band_quality.json").write_text(json.dumps(
            {"deck": deck, "khz": khz, "stage": "starting the listener…",
             "ts": time.time()}))
    except OSError:
        pass
    p = subprocess.Popen([PY, str(HERE / "am_listen.py"),
                          "--khz", str(khz), "--deck", deck,
                          "--antenna", ant, "--play"])
    LIVE_PROCS.append(p)


def am_hd_try(khz):
    """The 7/26-proven pipeline as a button: offset capture -> band filter
    -> cu8 (the only nrsc5 file dialect that works) -> albacore --am."""
    def work():
        BAND["am_hd"] = {"stage": f"capturing 45s @ {khz:g} kHz..."}
        try:
            import SoapySDR
            from scipy.signal import firwin, lfilter
            SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
            FSC = 2976750.0
            target = khz * 1e3
            center = target - 40e3          # DC spike stays out of channel
            d = SoapySDR.Device("driver=sdrplay")
            d.setSampleRate(SoapySDR.SOAPY_SDR_RX, 0, FSC)
            d.setFrequency(SoapySDR.SOAPY_SDR_RX, 0, center)
            try:
                d.setAntenna(SoapySDR.SOAPY_SDR_RX, 0, AM_ANT)
            except Exception:
                pass
            try:
                d.setGain(SoapySDR.SOAPY_SDR_RX, 0, "IFGR", 30)
            except Exception:
                pass
            st = d.setupStream(SoapySDR.SOAPY_SDR_RX, "CF32")
            d.activateStream(st)
            buf = np.zeros(262144, np.complex64)
            ch = []
            t0 = time.time()
            while time.time() - t0 < 45:
                r = d.readStream(st, [buf], len(buf), timeoutUs=800000)
                if r.ret > 0:
                    ch.append(buf[:r.ret].copy())
            d.deactivateStream(st)
            d.closeStream(st)
            x = np.concatenate(ch).astype(np.complex64)
            BAND["am_hd"] = {"stage": "filtering + cu8..."}
            nn = np.arange(len(x), dtype=np.float64)
            x = (x * np.exp(-2j * np.pi * (target - center) / FSC * nn)
                 ).astype(np.complex64)
            taps = firwin(301, 25e3 / (FSC / 2)).astype(np.float32)
            x = lfilter(taps, 1, x)[::2]          # -> exactly 1,488,375
            x = x / (np.abs(x).max() + 1e-12) * 0.85
            i16i = np.round(np.real(x) * 32767).astype(np.int16)
            i16q = np.round(np.imag(x) * 32767).astype(np.int16)
            cu8 = np.empty(2 * len(x), np.uint8)
            cu8[0::2] = ((i16i.astype(np.int32) >> 8) + 128
                         ).clip(0, 255).astype(np.uint8)
            cu8[1::2] = ((i16q.astype(np.int32) >> 8) + 128
                         ).clip(0, 255).astype(np.uint8)
            cap = LAB / "am_hd_try.cu8"
            cu8.tofile(cap)
            wav = LAB / "am_hd_try.wav"
            BAND["am_hd"] = {"stage": "albacore --am decoding..."}
            p = subprocess.run(
                [NRSC5, "--am", "-r", str(cap), "-o", str(wav), "0"],
                capture_output=True, text=True, timeout=240)
            lines = [l for l in (p.stderr or "").splitlines()
                     if l.strip()][:12]
            ok = wav.exists() and wav.stat().st_size > 100_000
            BAND["am_hd"] = {
                "stage": "done", "hd": ok, "log": lines,
                "verdict": ("HD DECODED — listen!" if ok else
                            "no HD sync (WSHE 820 wants MIDDAY - "
                            "4.3 kW day vs 430 W night)")}
            if ok:
                subprocess.Popen([MPV, str(wav), "--volume=100"])
        except Exception as e:
            BAND["am_hd"] = {"stage": "error", "verdict": str(e)[:120]}
    threading.Thread(target=work, daemon=True).start()


# â”€â”€ band survey â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def fm_power_sweep():
    """Wideband FFT hops across 88-108; returns {mhz: rssi_db} at the
    odd-tenth US channel frequencies."""
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    hops = [91.0, 97.0, 103.0]          # 8 MS/s each covers ~7 MHz well
    found = {}
    band_row = np.full(BAND_BINS, np.nan, np.float32)
    for hi, hop in enumerate(hops):
        SURVEY.update({"pct": 2 + int(12 * hi / len(hops)),
                       "cur_mhz": hop,
                       "line": f"sweeping {hop - 3.4:.1f}-"
                               f"{hop + 3.4:.1f} MHz for carriers "
                               f"(hop {hi + 1}/{len(hops)})"})
        # the idle band sweeper may take ~1 s to yield the device
        sdr = st = None
        for attempt in range(5):
            try:
                sdr, st = open_sdr(hop, ifgr=59, rfgain="3",
                                   rate=8_000_000)
                break
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(2.0)
        buf = np.empty(2 * 65536, np.int16)
        acc = None
        N = 8192
        for _ in range(24):
            r = sdr.readStream(st, [buf], 65536, timeoutUs=300000)
            if r.ret != 65536:
                continue
            x = (buf[0:2 * N:2].astype(np.float32)
                 + 1j * buf[1:2 * N:2].astype(np.float32))
            psd = np.abs(np.fft.fftshift(np.fft.fft(
                x * np.hanning(N))))**2
            acc = psd if acc is None else acc + psd
        close_sdr(sdr, st)
        if acc is None:
            continue
        fax = np.fft.fftshift(np.fft.fftfreq(N, 1 / 8e6)) / 1e6 + hop
        db = 10 * np.log10(acc + 1e-12)
        # feed the idle waterfall so the display stays alive mid-scan
        use = np.abs(fax - hop) < 3.4
        bi = ((fax[use] * 1e6 - BAND_LO) / (BAND_HI - BAND_LO)
              * BAND_BINS).astype(int)
        ok = (bi >= 0) & (bi < BAND_BINS)
        np.fmax.at(band_row, bi[ok], db[use][ok].astype(np.float32))
        if np.isfinite(band_row).any():
            fill = float(np.nanmin(band_row))
            BAND["rows"].append(
                {"t": round(time.time(), 3),
                 "db": np.round(np.where(np.isfinite(band_row),
                                         band_row, fill), 1).tolist()})
        floor = float(np.median(db))
        f0 = 88.1
        while f0 <= 107.9 + 1e-9:
            if hop - 3.4 <= f0 <= hop + 3.4:
                m = np.abs(fax - f0) < 0.06
                if m.any():
                    v = float(db[m].max() - floor)
                    if f0 not in found or v > found[f0]:
                        found[round(f0, 1)] = round(v, 1)
            f0 = round(f0 + 0.2, 1)
    return found


def hd_probe(mhz, secs=8):
    """Capture briefly, run nrsc5, scrape identity + programs."""
    sdr, st = open_sdr(mhz, ifgr=59, rfgain="3")
    n_want = int(secs * FS_CAP)
    buf = np.empty(2 * 65536, np.int16)
    iq = LAB / "probe.cu8"
    got = 0
    with open(iq, "wb") as f:
        while got < n_want:
            r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
            if r.ret > 0:
                n = min(r.ret, n_want - got)
                f.write(cs16_to_cu8(decimate2(buf[:2 * n])).tobytes())
                got += n
    close_sdr(sdr, st)
    info = {"hd": False, "name": None, "slogan": None, "programs": {},
            "mer_lo": None, "mer_hi": None, "ber": None}
    aas = LAB / "aas_guide" / f"{mhz:.1f}"
    aas.mkdir(parents=True, exist_ok=True)
    keeper = subprocess.Popen(["powershell", "-NoProfile", "-Command",
                               "Start-Sleep -Seconds 90"],
                              stdout=subprocess.PIPE)
    p = subprocess.Popen([NRSC5, "-r", str(iq),
                          "--dump-aas-files", str(aas), str(0)],
                         stdin=keeper.stdout, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         errors="replace", env=_nrsc5_env())
    t0 = time.time()

    def reader():
        for line in p.stdout:
            parse_nrsc5_line(line, info)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    while time.time() - t0 < 25 and th.is_alive():
        time.sleep(0.5)
    try:
        p.terminate()
        keeper.terminate()
    except Exception:
        pass
    return info


def parse_nrsc5_line(line, info):
    line = line.strip()
    if "Synchronized" in line:
        info["hd"] = True
        info["sync"] = True
    m = re.search(r"Station name: (.+)", line)
    if m:
        info["name"] = m.group(1).strip()
    m = re.search(r"Slogan: (.+)", line)
    if m:
        info["slogan"] = m.group(1).strip()
    m = re.search(r"Audio program (\d+): (.+?), type: (\w+)", line)
    if m:
        info.setdefault("programs", {})[m.group(1)] = m.group(3)
    m = re.search(r"MER: ([-\d.]+) dB \(lower\), ([-\d.]+) dB \(upper\)",
                  line)
    if m:
        info["mer_lo"] = float(m.group(1))
        info["mer_hi"] = float(m.group(2))
    m = re.search(r"BER: ([\d.]+)", line)
    if m:
        info["ber"] = float(m.group(1))
    m = re.search(r"Title: (.+)", line)
    if m:
        info["title"] = m.group(1).strip()
    m = re.search(r"Artist: (.+)", line)
    if m:
        info["artist"] = m.group(1).strip()
    m = re.search(r"Album: (.+)", line)
    if m:
        info["album"] = m.group(1).strip()
    m = re.search(r"Genre: (.+)", line)
    if m:
        info["genre"] = m.group(1).strip()
    m = re.search(r"Message: (.+)", line)
    if m:
        info["message"] = m.group(1).strip()
    m = re.search(r"Station location: ([-\d.]+), ([-\d.]+)", line)
    if m:
        info["tower"] = f"{m.group(1)}, {m.group(2)}"
    m = re.search(r"Alert: (.+)", line)
    if m:
        info["alert"] = m.group(1).strip()
    if "Alert ended" in line:
        info["alert"] = None


def run_survey():
    if SURVEY["running"]:
        return
    SURVEY.update({"running": True, "pct": 2,
                   "line": "sweeping the bandâ€¦"})
    BAND["hold"] = True                  # bench the idle sweeper
    try:
        stop_listen()
        time.sleep(2.5)                  # let the sweeper finish its hop
        carriers = fm_power_sweep()
        strong = {f: v for f, v in carriers.items() if v >= 14}
        SURVEY.update({"pct": 15,
                       "line": f"{len(strong)} strong stations â€” "
                               "probing for HDâ€¦"})
        stations = []
        done = 0
        n_hd = 0
        # probe cache (speedup, 7/20): a strong carrier that proved
        # non-HD in the last ~20 h doesn't earn a fresh 10 s HD probe
        # every scan — reuse its verdict unless its RSSI moved. HD
        # stations ALWAYS re-probe (fresh MER/programs is the guide's
        # quality currency).
        prev = {}
        try:
            old = json.loads(STATIONS.read_text(encoding="utf-8"))
            age_h = (time.time() - time.mktime(time.strptime(
                old["surveyed_at"], "%Y-%m-%dT%H:%M:%S"))) / 3600
            if age_h < 20:
                prev = {round(s["mhz"], 1): s for s in old["stations"]}
        except Exception:
            pass
        for mhz, rssi in sorted(strong.items()):
            SURVEY["cur_mhz"] = mhz
            SURVEY["line"] = (f"probing {mhz:.1f} MHz for HD "
                              f"({done + 1}/{len(strong)}) - "
                              f"{n_hd} HD found so far")
            oldent = prev.get(round(mhz, 1))
            if oldent and not oldent.get("hd") \
                    and abs((oldent.get("rssi") or -99) - rssi) < 8:
                info = {k: oldent.get(k) for k in
                        ("hd", "name", "slogan", "programs", "mer_lo",
                         "mer_hi", "ber", "genre", "message", "tower")}
                info["hd"] = False
                SURVEY["line"] = f"{mhz:.1f}: non-HD (cached verdict)"
            else:
                info = hd_probe(mhz)
            done += 1
            if info.get("hd"):
                n_hd += 1
                SURVEY["line"] = (f"{mhz:.1f}: "
                                  f"{info.get('name') or 'HD station'} "
                                  f"decoded ({done}/{len(strong)})")
            SURVEY["pct"] = 15 + int(80 * done / max(1, len(strong)))
            logos = sorted((LAB / "aas_guide" / f"{mhz:.1f}").glob("*.png")) \
                + sorted((LAB / "aas_guide" / f"{mhz:.1f}").glob("*.jp*g"))
            logos = sorted(logos, key=lambda p: p.stat().st_size)
            stations.append({"mhz": mhz, "rssi": rssi,
                             "hd": info.get("hd", False),
                             "name": info.get("name"),
                             "slogan": info.get("slogan"),
                             "programs": info.get("programs", {}),
                             "mer_lo": info.get("mer_lo"),
                             "mer_hi": info.get("mer_hi"),
                             "ber": info.get("ber"),
                             "genre": info.get("genre"),
                             "message": info.get("message"),
                             "tower": info.get("tower"),
                             "logo": logos[0].name if logos else None})
            STATIONS.write_text(json.dumps(
                {"surveyed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "stations": stations}, indent=1), encoding="utf-8")
        SURVEY.update({"pct": 100, "line": "survey complete"})
    except Exception as e:
        SURVEY["line"] = f"survey failed: {e}"
    finally:
        SURVEY["running"] = False
        SURVEY["cur_mhz"] = None
        BAND["hold"] = False


# â”€â”€ listening â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _cal_gains(mhz, ifgr, rfgain):
    """Learned per-station gains (hd_quality.py sweeps) beat the one-size
    default - at the HD cliff the measured optimum is the difference
    between music and static (wired in 2026-07-18, the GAINS-table law)."""
    try:
        cal = json.loads((LAB / "hd_gain_cal.json").read_text())
        c = cal.get(f"{mhz:.1f}")
        if c and c.get("mer_db") is not None:
            return float(c["ifgr"]), str(c["rfgain"])
    except Exception:
        pass
    return ifgr, str(rfgain)


ANT_PICK = {"auto": None, "a": "Antenna A", "b": "Antenna B",
            "c": "Antenna C"}


def listen(mhz, prog, name, ifgr=59, rfgain="3", antenna=None):
    ifgr, rfgain = _cal_gains(mhz, ifgr, rfgain)
    STATE["ifgr"], STATE["rfgain"] = ifgr, str(rfgain)
    stop_listen()
    time.sleep(1)
    with LOCK:
        GEN[0] += 1
        my_gen = GEN[0]
        STATE.update({"mhz": mhz, "prog": prog, "name": name,
                      "listening": True, "sync": False, "audio": None,
                      "title": None, "artist": None})

    set_stage(8, "warming the tubes â€” opening the radio")

    def worker():
        sdr = st = None
        ant = antenna or pick_antenna(mhz, "hd")
        STATE["antenna"] = ANT_NICK.get(ant, ant) \
            + ("" if antenna is None else " [manual]")
        for attempt in range(5):          # post-restart contention retry
            try:
                sdr, st = open_sdr(mhz, ifgr=ifgr, rfgain=str(rfgain),
                                   ant=ant)
                break
            except Exception:
                if GEN[0] != my_gen:
                    return
                if attempt == 2:
                    set_stage(8, "radio service wedged - self-healing "
                                 "(one moment)")
                    heal_sdr_service()
                    continue
                set_stage(8, f"radio busy â€” retrying ({attempt + 2}/5)")
                time.sleep(2.5)
        if sdr is None:
            set_stage(0, "RADIO UNAVAILABLE â€” another process holds the "
                         "SDR; stop it and click again")
            STATE.update({"listening": False})
            return
        set_stage(30, "receiving â€” streaming into the HD decoder")
        # ONE FILE PER SESSION (the analog-then-HD static bug, 7/20):
        # on Windows the previous session's player still holds
        # radio_live.wav, the unlink fails SILENTLY, and the new
        # decoder collides with the old file's bytes/size — the size
        # gate passed instantly on stale content and mpv played the
        # corpse. Unique names make collision impossible; old files
        # are swept best-effort (locked ones die with their player).
        wav = LAB / f"live_{my_gen}.wav"
        for old in LAB.glob("live_*.wav"):
            if old != wav:
                try:
                    old.unlink()
                except OSError:
                    pass
        STATE["wav"] = wav.name
        buf = np.empty(2 * 65536, np.int16)
        # STREAMING (2026-07-05): nrsc5 -r - reads IQ from stdin, so the
        # radio pumps straight into the decoder â€” no growing-file EOF
        # stall (this build stops at EOF instead of tailing).
        aas = LAB / "aas"
        aas.mkdir(exist_ok=True)
        for old in aas.glob("*"):
            try:
                old.unlink()
            except OSError:
                pass
        # AUDIO OVER A PIPE (7/20, the ear-static bug): a player tailing
        # the growing WAV stutter-loops whenever it catches the live
        # edge — the ear hears heavy static while the FILE meters clean
        # (hd_listen.py's law; this panel was the last place still
        # tailing). nrsc5 now writes audio to stdout; audio_tee copies
        # it to the session WAV (meters + cast) and, once the quality
        # gate opens, into mpv's stdin as a continuous stream.
        nr = subprocess.Popen(
            [NRSC5, "-r", "-", "-o", "-",
             "--dump-aas-files", str(aas), str(prog)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=False, env=_nrsc5_env())
        LIVE_PROCS.append(nr)
        STATE["decoder"] = DECODER_TAG
        info = {}

        def audio_tee():
            with open(wav, "wb") as f:
                while True:
                    chunk = nr.stdout.read(8192)
                    if not chunk:
                        return
                    f.write(chunk)
                    m = PLAYER["mpv"]
                    if m is not None:
                        try:
                            m.stdin.write(chunk)
                            m.stdin.flush()
                        except (BrokenPipeError, OSError):
                            PLAYER["mpv"] = None
        threading.Thread(target=audio_tee, daemon=True).start()

        def scrape():
            for raw_line in nr.stderr:
                try:
                    line = raw_line.decode("utf-8", "replace")
                except AttributeError:
                    line = raw_line
                parse_nrsc5_line(line, info)
                for k in ("title", "artist", "album", "genre",
                          "message", "tower", "alert",
                          "mer_lo", "mer_hi", "ber", "sync"):
                    if k in info:
                        STATE[k] = info[k]
        threading.Thread(target=scrape, daemon=True).start()

        def bank_logo():
            """Logos arrive over ~30-90 s of AAS — too slow for the
            25 s scan probe, free while actually listening. File the
            smallest image into the guide so the grid fills in
            organically as stations get played."""
            time.sleep(45)
            if GEN[0] != my_gen:
                return
            try:
                imgs = sorted(list(aas.glob("*.png"))
                              + list(aas.glob("*.jp*g")),
                              key=lambda p: p.stat().st_size)
                if not imgs:
                    return
                gdir = LAB / "aas_guide" / f"{mhz:.1f}"
                gdir.mkdir(parents=True, exist_ok=True)
                dest = gdir / imgs[0].name
                dest.write_bytes(imgs[0].read_bytes())
                st = json.loads(STATIONS.read_text(encoding="utf-8"))
                for s in st["stations"]:
                    if abs(s["mhz"] - mhz) < 0.05:
                        s["logo"] = imgs[0].name
                STATIONS.write_text(json.dumps(st, indent=1),
                                    encoding="utf-8")
            except Exception:
                pass
        threading.Thread(target=bank_logo, daemon=True).start()
        set_stage(45, "decoder hunting sync")
        nr_t0 = time.time()
        # attached is session-LOCAL on purpose: PLAYER is a global slot
        # that casting (and the next session) may null — reading it in
        # the fall logic let a stale worker hijack a fresh click into
        # analog, and would end a cast in a bogus fallback after 45 s
        attached = False
        low_mer_since = None
        # LOSSLESS PUMP (2026-07-05): the SDR loop must NEVER block on the
        # decoder's pipe â€” backpressure was stalling reads, dropping
        # samples, and turning clean BER into static audio. Reader only
        # reads; a writer thread absorbs pipe stalls via a deep queue.
        q = queue.Queue(maxsize=256)

        def feeder():
            while GEN[0] == my_gen:
                try:
                    chunk = q.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    nr.stdin.write(
                        cs16_to_cu8(decimate2(chunk)).tobytes())
                except (OSError, ValueError):
                    return
        threading.Thread(target=feeder, daemon=True).start()

        def on_static():
            set_stage(30, "audio probe says STATIC â€” HD stream is lying; "
                          "switching to analog FMâ€¦")
            threading.Thread(target=listen_fm, args=(mhz, name),
                             daemon=True).start()
        threading.Thread(target=audio_watch,
                         args=(my_gen, wav, on_static),
                         daemon=True).start()
        last_hb = time.time()
        while GEN[0] == my_gen:
            if time.time() - last_hb > 20:
                try:
                    import radio_lock
                    radio_lock.heartbeat()
                    why = radio_lock.should_yield()
                    if why:
                        # a satellite pass (prio 100) outranks the
                        # human (80): losing 15 min of music beats
                        # losing an unrepeatable pass (8:55 PM 7/20
                        # was lost exactly this way). Full stop so
                        # no player loops a stale file.
                        stop_listen()
                        set_stage(0, f"antenna yielded: {why} — "
                                     f"retune when the pass ends")
                        break
                except Exception:
                    pass
                last_hb = time.time()
            r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
            if r.ret > 0:
                n = r.ret - (r.ret & 1)      # keep I/Q pairing even
                try:
                    q.put_nowait(buf[:2 * n].copy())
                except queue.Full:
                    pass                     # decoder hopeless behind; skip
                spec_feed(buf[:2 * n])       # throttled copy, FFT elsewhere
            if STATE.get("sync") and STATE["pct"] < 70:
                set_stage(70, "SYNC â€” decoding digital audio")
                t_sync = time.time()
            # honesty + rescue, THREE ways a click must not end in noise
            # or silence (stress-tested 7/20: sync alone is NOT audio):
            #  (1) no sync in 25 s               -> analog
            #  (2) synced but no audio in 20 s   -> analog (105.1 hung
            #      forever at "decoding"; junk syncs at MER -6 too)
            #  (3) synced but MER below the audio cliff for 12 s ->
            #      analog (104.1 played garble at MER 8.4)
            fall = None
            if not STATE.get("sync") and time.time() - nr_t0 > 25:
                fall = "no HD sync â€” digital too weak here"
            elif STATE.get("sync") and not attached \
                    and time.time() - nr_t0 > 45:
                fall = "HD synced but no audio is decoding"
            elif not attached and STATE.get("sync") \
                    and (STATE.get("mer_lo") or 99) < 9.5:
                low_mer_since = low_mer_since or time.time()
                if time.time() - low_mer_since > 12:
                    fall = (f"HD too close to the cliff here "
                            f"(MER {STATE.get('mer_lo')})")
            else:
                low_mer_since = None
            if fall:
                if GEN[0] != my_gen:
                    # a newer click owns the radio — a stale worker
                    # must never spawn a fallback over it (this race
                    # turned an HD2 click into analog, 7/20)
                    break
                set_stage(30, fall + "; switching to analog FMâ€¦")
                close_sdr(sdr, st)
                try:
                    nr.terminate()
                except Exception:
                    pass
                threading.Thread(target=listen_fm, args=(mhz, name),
                                 daemon=True).start()
                return
            if not attached and STATE.get("sync") \
                    and (STATE.get("mer_lo") or 0) >= 9.5 \
                    and wav.exists() and wav.stat().st_size > 400_000:
                # audio gates on SYNC + MER above the cliff + real
                # audio bytes: sync alone is not audio (stress-tested).
                # The player joins the PIPE at the live edge — never
                # the file (growing-file tailing = the ear-static bug).
                set_stage(88, "buffering audio")
                m = subprocess.Popen(
                    [MPV] + MPV_PIPE_ARGS
                    + [f"--title=Radio Tuna â€” {name}"],
                    stdin=subprocess.PIPE)
                LIVE_PROCS.append(m)
                PLAYER["mpv"] = m
                attached = True
                set_stage(100, "")
        try:
            nr.stdin.close()
        except Exception:
            pass
        close_sdr(sdr, st)

    threading.Thread(target=worker, daemon=True).start()


def listen_fm(mhz, name, ifgr=59, rfgain="3", antenna=None):
    """Analog FM v2 (fm_stereo.py): channel-select FIR, pilot-locked
    stereo with SNR-adaptive mono blend, 15 kHz audio filtering, live
    truth dials. The v1 path shipped the whole unfiltered composite
    (0-46 kHz) into the WAV — that WAS the hiss."""
    stop_listen()
    time.sleep(1)
    with LOCK:
        GEN[0] += 1
        my_gen = GEN[0]
        STATE.update({"mhz": mhz, "prog": None, "name": name + " (analog)",
                      "listening": True, "sync": False, "audio": None,
                      "title": name, "artist": "analog FM â€” stereo v2",
                      "mer_lo": None, "mer_hi": None, "ber": None,
                      "decoder": "fm_stereo v2 (blend)"})
    set_stage(15, "opening the radio (analog FM)")
    STATE["ifgr"], STATE["rfgain"] = ifgr, str(rfgain)

    def worker():
        sdr = st = None
        ant = antenna or pick_antenna(mhz, "fm")
        STATE["antenna"] = ANT_NICK.get(ant, ant) \
            + ("" if antenna is None else " [manual]")
        for attempt in range(5):
            try:
                sdr, st = open_sdr(mhz, ifgr=ifgr, rfgain=str(rfgain),
                                   ant=ant)
                break
            except Exception:
                if GEN[0] != my_gen:
                    return
                if attempt == 2:
                    set_stage(15, "radio service wedged - self-healing "
                                  "(one moment)")
                    heal_sdr_service()
                    continue
                set_stage(15, f"radio busy â€” retrying ({attempt + 2}/5)")
                time.sleep(2.5)
        if sdr is None:
            set_stage(0, "RADIO UNAVAILABLE â€” another process holds the "
                         "SDR; stop it and click again")
            STATE.update({"listening": False})
            return
        set_stage(55, "demodulating FM (stereo v2)")
        # one file per session — see the HD path's collision note
        wav = LAB / f"live_{my_gen}.wav"
        for old in LAB.glob("live_*.wav"):
            if old != wav:
                try:
                    old.unlink()
                except OSError:
                    pass
        STATE["wav"] = wav.name
        fh = open(wav, "wb")
        fh.write(fm_stereo.wav_header(fm_stereo.FS_AUDIO, 2))
        fh.flush()
        dem = fm_stereo.FMStereo()
        dem.tap_secs = 10.0            # live RDS reads the composite

        def rds_watch():
            """Every ~12 s decode RDS from the composite tap: station
            name (PS) + RadioText (= now-playing for analog FM)."""
            import rds as rdsmod
            time.sleep(14)
            while GEN[0] == my_gen:
                try:
                    if dem.tap:
                        mpx = np.concatenate(dem.tap)
                        rec, dfs = rdsmod.costas_bpsk(mpx, fm_stereo.FSC)
                        best = {"groups": 0}
                        for sgn in (rec, -rec):
                            bits = rdsmod.bits_from_symbols(sgn, dfs)
                            for fl in (bits, bits ^ 1):
                                g = rdsmod.find_blocks(fl)
                                if len(g) > best["groups"]:
                                    best = rdsmod.decode_groups(g)
                        if best["groups"] > 2 and GEN[0] == my_gen:
                            ps = best.get("ps") or ""
                            rt = best.get("rt") or ""
                            if rt:
                                STATE["title"] = rt
                            if ps:
                                STATE["artist"] = (f"{ps} · RDS"
                                                   + (f" · {best['pty']}"
                                                      if best.get("pty")
                                                      else ""))
                except Exception:
                    pass
                time.sleep(12)
        threading.Thread(target=rds_watch, daemon=True).start()
        mpv = None
        t0 = time.time()
        # reader thread does NOTHING but big-gulp reads (the starvation
        # law); the demod runs at its leisure off a deep queue
        iq_q = queue.Queue(maxsize=64)

        def sdr_reader():
            while GEN[0] == my_gen:
                b = np.empty(2 * 262144, np.int16)
                r = sdr.readStream(st, [b], 262144, timeoutUs=1000000)
                if r.ret > 0:
                    try:
                        iq_q.put_nowait(b[:2 * r.ret])
                    except queue.Full:
                        pass
        threading.Thread(target=sdr_reader, daemon=True).start()
        last_hb = time.time()
        t_open = time.time()
        while GEN[0] == my_gen:
            if time.time() - last_hb > 20:
                try:
                    import radio_lock
                    radio_lock.heartbeat()
                    why = radio_lock.should_yield()
                    if why:
                        stop_listen()
                        set_stage(0, f"antenna yielded: {why} — "
                                     f"retune when the pass ends")
                        break
                except Exception:
                    pass
                last_hb = time.time()
            # honesty gate: if after settling the pilot is buried, this
            # analog is NOISE — say so and stop rather than playing hiss
            # at the human (half the scan's HD finds are 50-mile DC
            # stations whose analog is unlistenable here; their failed
            # HD used to "fall back" into pure static)
            if time.time() - t_open > 6 and mpv is None:
                # MONO stations have no 19 kHz pilot at all (105.9 WMAL
                # talk, 7/20): pilot-only gating called a strong mono
                # analog "out of reach". Noise = BOTH dials low; a mono
                # program keeps audio_snr high with pilot buried.
                p_snr = STATE.get("pilot_snr_db")
                a_snr = STATE.get("audio_snr_db")
                if (p_snr is not None and p_snr < 7
                        and (a_snr is None or a_snr < 12)):
                    set_stage(0, f"{mhz:.1f} is out of reach here "
                                 f"(pilot {p_snr:.0f} dB, audio "
                                 f"{a_snr if a_snr is not None else '?'} dB)"
                                 f" — pick a station with a green grade")
                    STATE.update({"listening": False})
                    break
            try:
                chunk = iq_q.get(timeout=1.0)
            except queue.Empty:
                continue
            spec_feed(chunk)                 # throttled copy, FFT elsewhere
            pcm, tele = dem.feed(decimate2(chunk))
            if len(pcm):
                fh.write(pcm.tobytes())
                fh.flush()
            for k in FM_KEYS:
                if k in tele:
                    STATE[k] = tele[k]
            if mpv is None and time.time() - t0 > 2.5 \
                    and ((STATE.get("pilot_snr_db") or -99) >= 7
                         or (STATE.get("audio_snr_db") or -99) >= 12):
                mpv = subprocess.Popen(
                    [MPV, str(wav), "--volume=100", "--keep-open=yes",
                     "--force-seekable=yes",
                     f"--title=ALBACORE TUNA â€” {name} (FM)"])
                LIVE_PROCS.append(mpv)
                set_stage(100, "")
        fh.close()
        close_sdr(sdr, st)

    threading.Thread(target=worker, daemon=True).start()


# â”€â”€ the page â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>RADIO TUNA</title><style>
body{font-family:Consolas,'Lucida Console',monospace;color:#9fd4e0;
margin:0;padding:18px;min-height:100vh;background:#030509;
background-image:linear-gradient(rgba(0,229,255,.05) 1px,transparent 1px),
linear-gradient(90deg,rgba(0,229,255,.05) 1px,transparent 1px);
background-size:44px 44px}
body::after{content:'';position:fixed;inset:0;pointer-events:none;
background:repeating-linear-gradient(0deg,rgba(0,0,0,.14) 0 1px,
transparent 1px 3px)}
h1{font-size:24px;margin:0;letter-spacing:4px;color:#00e5ff;
text-shadow:0 0 14px rgba(0,229,255,.8),0 0 40px rgba(0,229,255,.35)}
h1 .mag{color:#ff2bd6;text-shadow:0 0 14px rgba(255,43,214,.8)}
.sub{color:#3f6a78;font-size:11px;margin-bottom:14px;letter-spacing:2px}
#cabinet{max-width:980px;margin:0 auto;background:rgba(6,11,20,.92);
border:1px solid rgba(0,229,255,.35);border-radius:8px;
box-shadow:0 0 24px rgba(0,229,255,.12),inset 0 0 60px rgba(0,0,0,.5);
padding:20px}
#freq{font-size:52px;color:#00e5ff;text-align:center;
text-shadow:0 0 22px rgba(0,229,255,.75);margin:6px 0}
#nowplaying{text-align:center;min-height:44px;color:#c8ecf4}
#nowplaying .t{font-size:19px}
#nowplaying .a{font-size:13px;color:#ff2bd6}
#dial{position:relative;height:64px;background:#02040a;
border:1px solid rgba(0,229,255,.35);border-radius:6px;margin:14px 0}
#dial canvas{width:100%;height:100%;display:block}
.meters{display:flex;gap:12px;justify-content:center;margin:10px 0;
flex-wrap:wrap}
.meter{background:#04070f;border:1px solid rgba(0,229,255,.3);
border-radius:6px;padding:6px 14px;text-align:center;min-width:86px}
.meter .k{font-size:10px;color:#3f6a78;letter-spacing:2px}
.meter .v{font-size:20px;color:#00e5ff;text-shadow:0 0 10px
rgba(0,229,255,.5)}
button{cursor:pointer;font-family:Consolas,monospace}
.knob{background:#04070f;color:#9fd4e0;border:1px solid #00e5ff;
border-radius:4px;padding:7px 18px;font-size:13px;letter-spacing:1px}
.knob:hover{box-shadow:0 0 14px rgba(0,229,255,.6);color:#fff}
.knob.hot{border-color:#ff2bd6;color:#ff8fe8}
.knob.hot:hover{box-shadow:0 0 14px rgba(255,43,214,.6)}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:12px}
td{padding:7px 8px;border-bottom:1px solid rgba(0,229,255,.12);
vertical-align:middle}
tr:hover td{background:rgba(0,229,255,.04)}
.st{font-size:15px;color:#c8ecf4}
.hd{display:inline-block;background:#ff2bd6;color:#05070d;font-weight:
bold;font-size:10px;border-radius:3px;padding:1px 6px;margin-left:6px;
box-shadow:0 0 8px rgba(255,43,214,.6)}
.prog{background:#04070f;color:#9fd4e0;border:1px solid
rgba(0,229,255,.45);border-radius:4px;padding:4px 12px;margin:2px;
font-size:12px}
.prog:hover{box-shadow:0 0 10px rgba(0,229,255,.55);color:#fff}
.rssi{color:#3f6a78;font-size:12px}
#status{text-align:center;color:#7ab8c8;font-size:13px;min-height:20px;
margin-top:6px}
#pbar{height:6px;background:#04070f;border:1px solid rgba(0,229,255,.3);
border-radius:4px;margin:6px 15%;overflow:hidden;display:none}
#pbar div{height:100%;background:linear-gradient(90deg,#00e5ff,#ff2bd6);
transition:width .8s;box-shadow:0 0 8px rgba(0,229,255,.8)}
#nerd{margin-top:14px;border:1px solid rgba(255,43,214,.35);
border-radius:6px;background:#04070f}
#nerd summary{cursor:pointer;padding:8px 12px;color:#ff2bd6;
letter-spacing:3px;font-size:12px;text-shadow:0 0 10px
rgba(255,43,214,.5)}
#nerdgrid{display:grid;grid-template-columns:repeat(auto-fill,
minmax(150px,1fr));gap:8px;padding:10px}
.ncard{border:1px solid rgba(0,229,255,.25);border-radius:4px;
padding:6px 10px;background:rgba(0,229,255,.03)}
.ncard .k{font-size:9px;color:#3f6a78;letter-spacing:2px}
.ncard .v{font-size:16px;color:#39ff8a;text-shadow:0 0 8px
rgba(57,255,138,.4)}
.nbar{height:5px;background:#02040a;border-radius:3px;margin-top:4px;
overflow:hidden}
.nbar div{height:100%;background:linear-gradient(90deg,#00e5ff,#39ff8a);
transition:width .6s}
#daylab{padding:6px 12px;color:#7ab8c8;font-size:11px;
border-top:1px solid rgba(255,43,214,.2);white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
/* ── RADIO TUNA masthead + decks (2026-07-26 unification) ── */
#masthead{max-width:980px;margin:0 auto 10px;display:flex;align-items:baseline;
gap:18px;flex-wrap:wrap}
#masthead .brand{font-size:26px;letter-spacing:5px;color:#e8f6fa;
text-shadow:0 0 16px rgba(0,229,255,.5)}
#masthead .brand b{color:#00e5ff}
.decktabs{display:flex;gap:8px;margin-left:auto}
.decktab{cursor:pointer;padding:6px 16px;border-radius:6px 6px 0 0;
font-size:13px;letter-spacing:2px;border:1px solid #23404d;color:#557;
background:rgba(8,14,22,.8);user-select:none}
.decktab.fm.on{color:#00e5ff;border-color:rgba(0,229,255,.6);
text-shadow:0 0 10px rgba(0,229,255,.7)}
.decktab.am.on{color:#ffb457;border-color:rgba(255,180,87,.6);
text-shadow:0 0 10px rgba(255,180,87,.7)}
.decktab.sw.on{color:#f0e4c0;border-color:rgba(200,180,130,.6);
text-shadow:0 0 10px rgba(240,222,170,.55)}
.deck{display:none}.deck.on{display:block}
/* AM deck — tube-glow nightwave */
#cab-am{max-width:980px;margin:0 auto;padding:20px;border-radius:10px;
background:radial-gradient(ellipse at 50% 0%,rgba(80,40,8,.45),rgba(10,5,2,.95) 70%),#0d0703;
border:1px solid rgba(255,180,87,.4);
box-shadow:0 0 26px rgba(255,150,50,.14),inset 0 0 70px rgba(0,0,0,.6);
color:#e8c9a0;font-family:Consolas,monospace}
#cab-am h2{margin:0;color:#ffb457;letter-spacing:4px;font-size:20px;
text-shadow:0 0 14px rgba(255,180,87,.8),0 0 44px rgba(255,120,20,.3)}
#cab-am .sub{color:#8a6a45}
#am-freq{font-size:48px;text-align:center;color:#ffcf87;margin:8px 0;
text-shadow:0 0 22px rgba(255,180,87,.8)}
#cab-am table{width:100%;border-collapse:collapse;font-size:13px}
#cab-am th{color:#8a6a45;text-align:left;padding:4px 8px;letter-spacing:1px;
border-bottom:1px solid rgba(255,180,87,.25)}
#cab-am td{padding:4px 8px;border-bottom:1px solid rgba(255,180,87,.1)}
#cab-am tr:hover{background:rgba(255,150,50,.07)}
.ambtn{cursor:pointer;background:#2a180a;color:#ffcf87;border:1px solid
rgba(255,180,87,.5);border-radius:5px;padding:4px 12px;font-family:inherit}
.ambtn:hover{background:#3d2410;box-shadow:0 0 10px rgba(255,150,50,.3)}
#am-hdbox{margin-top:10px;padding:10px;border:1px dashed rgba(255,180,87,.4);
border-radius:6px;color:#d8b285;font-size:12px;min-height:20px}
.ambar{display:inline-block;height:9px;background:linear-gradient(90deg,#7a4a12,#ffb457);
border-radius:3px;vertical-align:middle}
/* SW deck — world-band phosphor */
/* WORLDBAND: WW2 listening-post noir — black wrinkle-finish steel,
   aged ivory dial lamps, oxidized brass, one deep signal-red accent.
   The spy's band, with 2026 instruments bolted in. */
#cab-sw{max-width:980px;margin:0 auto;padding:22px;border-radius:4px;
background:
 radial-gradient(ellipse 130% 90% at 50% 0%,rgba(240,222,170,.07),transparent 55%),
 radial-gradient(ellipse 160% 120% at 50% 110%,transparent 40%,rgba(0,0,0,.75)),
 #0a0908;
border:1px solid #4a4030;outline:1px solid #171310;outline-offset:-5px;
box-shadow:0 0 30px rgba(0,0,0,.8),inset 0 0 90px rgba(0,0,0,.7),
 inset 0 1px 0 rgba(240,222,170,.08);
color:#cfc4a0;font-family:'Courier New',Consolas,monospace}
#cab-sw h2{margin:0;color:#f0e4c0;letter-spacing:7px;font-size:19px;
font-weight:bold;text-shadow:0 0 12px rgba(240,222,170,.45);
text-transform:uppercase}
#cab-sw .sub{color:#8a7f60;letter-spacing:1px;font-style:italic}
#sw-bands{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}
.swband{cursor:pointer;padding:5px 12px;border:1px solid rgba(200,180,130,.35);
border-radius:2px;color:#c9bd97;background:#141210;font-size:12px;
letter-spacing:2px}
.swband.on,.swband:hover{color:#f0e4c0;background:#26211a;
border-color:#8a7f60;box-shadow:inset 0 0 8px rgba(0,0,0,.6),
 0 0 8px rgba(240,222,170,.15)}
#cab-sw table{width:100%;border-collapse:collapse;font-size:13px}
#cab-sw th{color:#8a7f60;text-align:left;padding:4px 8px;letter-spacing:2px;
text-transform:uppercase;font-size:11px;
border-bottom:1px double rgba(200,180,130,.35)}
#cab-sw td{padding:4px 8px;border-bottom:1px solid rgba(200,180,130,.1)}
#cab-sw tr:hover{background:rgba(240,222,170,.05)}
.swbtn{cursor:pointer;background:#171412;color:#e8dcb8;border:1px solid
rgba(200,180,130,.45);border-radius:2px;padding:4px 12px;font-family:inherit;
letter-spacing:1px}
.swbtn:hover{background:#242019;border-color:#b8483f;
box-shadow:0 0 8px rgba(184,72,63,.3)}
.swbar{display:inline-block;height:9px;
background:linear-gradient(90deg,#4a3f28,#d8c690);
border-radius:1px;vertical-align:middle;
box-shadow:inset 0 -1px 0 rgba(0,0,0,.5)}
</style></head><body>
<div id="masthead">
  <div class="brand">RADIO <b>TUNA</b></div>
  <div class="decktabs">
    <div class="decktab fm on" data-deck="fm" onclick="deck('fm')">FM · ALBACORE</div>
    <div class="decktab am" data-deck="am" onclick="deck('am')">AM · NIGHTWAVE</div>
    <div class="decktab sw" data-deck="sw" onclick="deck('sw')">SW · WORLDBAND</div>
  </div>
</div>
<div id="deck-fm" class="deck on"><div id="cabinet">
<h1>ALBACORE <span class="mag">TUNA</span> RADIO
<span style="font-size:13px">&#x1F41F;&#x26A1; high definition
receiver</span></h1>
<div class="sub">adaptive decoding // albacore core // the dials do not
lie</div>
<div style="margin:4px 0 10px">
 <button class="knob hot">FM &middot; HD</button>
 <button class="knob" style="opacity:.4" title="campaign pending">AM
 &mdash; soon</button>
 <button class="knob" style="opacity:.4" title="campaign pending">SW
 &mdash; soon</button>
 <select id="antsel" class="knob" style="float:right"
  title="AUTO = the measured tune table picks per station">
  <option value="auto">ANT: AUTO (measured)</option>
  <option value="a">ANT: rabbit ears (A)</option>
  <option value="b">ANT: Old Faithful (B)</option>
  <option value="c">ANT: discone (C)</option>
 </select>
</div>
<div id="alertbar" style="display:none;background:#5a0a0a;border:1px
solid #ff3b3b;color:#ffd0d0;padding:8px 14px;border-radius:6px;
margin:6px 0;text-shadow:0 0 8px rgba(255,59,59,.6)"></div>
<div id="freq">&mdash; &middot; &mdash;</div>
<div id="nowplaying" style="display:flex;gap:14px;align-items:center;
justify-content:center">
<img id="art" style="display:none;width:72px;height:72px;
border-radius:6px;border:1px solid rgba(0,229,255,.35)">
<div><span class="t">welcome</span><br>
<span class="a">survey the band, then click a program</span></div></div>
<div id="dial"><canvas id="dialc" width="1880" height="120"></canvas></div>
<div id="specbox" style="display:none;background:#02040a;border:1px solid
rgba(0,229,255,.35);border-radius:6px;margin:10px 0;padding:8px">
 <div id="speclabel" style="font-size:10px;color:#3f6a78;
 letter-spacing:2px">LIVE SPECTRUM &middot; &plusmn;250 kHz AROUND
 THE DIAL</div>
 <canvas id="specline" width="688" height="80"
  style="width:100%;height:80px;display:block"></canvas>
 <canvas id="wfall" width="688" height="150"
  style="width:100%;height:150px;display:block;margin-top:2px"></canvas>
</div>
<div class="meters">
 <div class="meter" id="hdqbox" style="display:none;min-width:150px">
  <div class="k">HD QUALITY</div><div class="v" id="hdq">&mdash;</div>
  <div style="height:6px;background:#02040a;border-radius:3px;
  overflow:hidden;margin-top:3px"><div id="hdqbar"
  style="height:100%;width:0%"></div></div></div>
 <div class="meter"><div class="k">MER LO</div><div class="v" id="mlo">&mdash;</div></div>
 <div class="meter"><div class="k">MER HI</div><div class="v" id="mhi">&mdash;</div></div>
 <div class="meter"><div class="k">BER</div><div class="v" id="ber">&mdash;</div></div>
 <div class="meter"><div class="k">LOCK</div><div class="v" id="lock">&mdash;</div></div>
 <div class="meter"><div class="k">AUDIO</div><div class="v" id="audio">&mdash;</div></div>
</div>
<div style="text-align:center">
 <button class="knob" onclick="survey()">&#x1F4E1; SURVEY THE BAND</button>
 <button class="knob" onclick="stopL()">&#x23F9; STOP</button>
 <button class="knob hot" id="castbtn" onclick="castToggle('fm')">&#x1F50A;
 CAST TO WI-FI SPEAKERS</button>
</div>
<div id="status"></div>
<div id="pbar"><div style="width:0%"></div></div>
<details id="nerd" open><summary>STATS FOR NERDS</summary>
<div id="nerdgrid"></div>
<div id="daylab"></div></details>
<div id="guide">loading the guide&hellip;</div>
</div></div>

<div id="deck-am" class="deck"><div id="cab-am">
  <h2>NIGHTWAVE <span style="color:#e8c9a0">·</span> MEDIUM WAVE</h2>
  <div class="sub">530–1700 kHz · K-180WLA loop · carrier-locked synchronous AM</div>
  <div id="am-freq">— kHz</div>
  <div id="am-quality" style="display:none;text-align:center;font-size:12px;
    color:#d8b285;margin:-4px 0 8px 0"></div>
  <canvas id="am-wf" width="384" height="90" style="display:none;width:100%;
    max-width:700px;margin:0 auto 8px;border:1px solid rgba(255,180,87,.25);
    border-radius:4px;image-rendering:pixelated"></canvas>
  <div style="text-align:center;margin-bottom:8px">
    <button class="ambtn" onclick="amScan()">⌁ SCAN THE BAND</button>
    <button class="ambtn" onclick="bandStop()">■ STOP</button>
    <button class="ambtn" onclick="castToggle('am')">🔊 CAST TO WI-FI SPEAKERS</button>
    <button class="ambtn" onclick="dxLog('am')">📖 DX LOG</button>
    <span id="am-status" style="margin-left:10px;color:#8a6a45"></span>
  </div>
  <div id="am-pbar" style="display:none;height:8px;max-width:700px;margin:0 auto 8px;
    border:1px solid rgba(255,180,87,.35);border-radius:4px;overflow:hidden">
    <div style="width:0%;height:100%;background:#c8863f;transition:width .8s"></div>
  </div>
  <div id="am-dx" style="display:none;margin-bottom:10px;padding:10px;
    border:1px dashed rgba(255,180,87,.4);border-radius:6px;font-size:12px"></div>
  <div id="am-hdbox">HD-AM: tune a station, then TRY HD — the 820 WSHE catch
    wants midday (4.3 kW day vs 430 W night).</div>
  <table><thead><tr><th>kHz</th><th>signal</th><th>who's there</th>
    <th></th><th></th></tr></thead><tbody id="am-rows">
    <tr><td colspan="5" style="color:#8a6a45">scan to light the dial…</td></tr>
  </tbody></table>
</div></div>

<div id="deck-sw" class="deck"><div id="cab-sw">
  <h2>Worldband Listening Post</h2>
  <div class="sub">shortwave · the spies' band since 1939, refitted with 2026 instruments
    · EiBi schedule = who is transmitting at THIS minute (UTC)</div>
  <div id="sw-bands"></div>
  <div style="margin-bottom:8px">
    <button class="swbtn" onclick="swScan()">⌁ SCAN BAND</button>
    <button class="swbtn" onclick="swScanAll()" style="font-weight:bold">🌍 SCAN ALL BANDS</button>
    <button class="swbtn" onclick="bandStop()">■ STOP</button>
    <button class="swbtn" onclick="castToggle('sw')">🔊 CAST TO WI-FI SPEAKERS</button>
    <button class="swbtn" onclick="dxLog('sw')">📖 DX LOG</button>
    <span id="sw-status" style="margin-left:10px;color:#8a7f60"></span>
    <div id="sw-quality" style="display:none;font-size:12px;color:#cfc4a0;
      margin-top:6px"></div>
    <div id="sw-pbar" style="display:none;height:8px;max-width:700px;margin:6px auto;
      border:1px solid rgba(200,190,160,.35);border-radius:4px;overflow:hidden">
      <div style="width:0%;height:100%;background:#b8a878;transition:width .8s"></div>
    </div>
    <canvas id="sw-wf" width="384" height="90" style="display:none;width:100%;
      max-width:700px;margin:6px auto;border:1px solid rgba(200,180,130,.25);
      border-radius:4px;image-rendering:pixelated"></canvas>
    <div id="sw-dx" style="display:none;margin-top:8px;padding:10px;
      border:1px dashed rgba(200,180,130,.4);border-radius:6px;font-size:12px"></div>
  </div>
  <div id="sw-sched" style="display:none;margin-bottom:10px;padding:10px;
    border:1px dashed rgba(200,180,130,.4);border-radius:6px;font-size:12px"></div>
  <table><thead><tr><th>kHz</th><th>signal</th><th>on air now (EiBi)</th>
    <th></th></tr></thead><tbody id="sw-rows">
    <tr><td colspan="4" style="color:#8a7f60">pick a band, scan the world…</td></tr>
  </tbody></table>
</div></div>

<script>
/* ── RADIO TUNA deck switching + AM/SW logic ── */
let CUR_DECK='fm', SW_BAND='31m';
function deck(d){CUR_DECK=d;
  document.querySelectorAll('.deck').forEach(e=>e.classList.remove('on'));
  document.getElementById('deck-'+d).classList.add('on');
  document.querySelectorAll('.decktab').forEach(e=>
    e.classList.toggle('on', e.dataset.deck===d));
  localStorage.setItem('rt_deck', d);}
function post(u,b){return fetch(u,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});}
function _scanPost(url,body,el,msg){
  post(url,body).then(r=>r.json()).then(v=>{
    document.getElementById(el).textContent=
      v==='busy'?'one scan at a time — the radio is taken':msg;
  }).catch(()=>{});}
function amScan(){_scanPost('/api/am/scan',null,'am-status','scanning 530–1700…');}
function swScan(){_scanPost('/api/sw/scan',{band:SW_BAND},'sw-status','scanning '+SW_BAND+'…');}
function swScanAll(){_scanPost('/api/sw/scan_all',null,'sw-status','world tour: sweeping 49m→16m…');}
function bandStop(){post('/api/stop');}
function bandListen(dk,khz){post('/api/band/listen',{deck:dk,khz:khz});
  document.getElementById(dk+'-status').textContent=
    'tuning '+khz.toFixed(0)+' kHz — audio in ~8 s…';
  if(dk==='am')document.getElementById('am-freq').textContent=khz.toFixed(0)+' kHz';}
function amHD(khz){post('/api/am/hd',{khz:khz});
  document.getElementById('am-hdbox').textContent='HD-AM: attempting '+khz+' kHz…';}
function swSched(khz){
  const box=document.getElementById('sw-sched');
  box.style.display='block';
  box.innerHTML='<span style="color:#8a7f60">loading schedule…</span>';
  fetch('/api/sw/sched?khz='+khz).then(r=>r.json()).then(j=>{
    if(!j.sched||!j.sched.length){
      box.innerHTML=`<b style="color:#e8dcb8">${khz.toFixed(0)} kHz</b> — no EiBi entries for this frequency. `+
        `<span style="color:#8a7f60;cursor:pointer" `+
        `onclick="document.getElementById('sw-sched').style.display='none'">[close]</span>`;return;}
    const now=new Date();
    const utc=now.getUTCHours()*100+now.getUTCMinutes();
    const rows=j.sched.map(e=>{
      const m=e.time.match(/(\\d{4})-(\\d{4})/)||['','0000','2400'];
      const a=+m[1],b=+m[2];
      const on=(a<=b)?(utc>=a&&utc<b):(utc>=a||utc<b);
      return `<tr style="${on?'color:#e8dcb8;font-weight:bold':'color:#4a8a5f'}">`+
      `<td>${m[1].slice(0,2)}:${m[1].slice(2)}–${m[2].slice(0,2)}:${m[2].slice(2)} UTC${on?' ●':''}</td>`+
      `<td>${e.station}</td><td>${e.lang||''}</td><td>${e.target||''}</td>`+
      `<td style="color:#9a8a5f">${e.tx||''}</td></tr>`;}).join('');
    box.innerHTML=`<div style="display:flex;justify-content:space-between;margin-bottom:6px">`+
      `<b style="color:#e8dcb8">📅 ${khz.toFixed(0)} kHz — full day (EiBi)</b>`+
      `<span style="color:#8a7f60;cursor:pointer" onclick="document.getElementById('sw-sched').style.display='none'">[close]</span></div>`+
      `<table style="width:100%"><thead><tr><th>time</th><th>station</th><th>lang</th><th>target</th><th>transmitter</th></tr></thead>`+
      `<tbody>${rows}</tbody></table>`;
  }).catch(()=>{box.innerHTML='schedule fetch failed';});}
const SWB=['49m','41m','31m','25m','22m','19m','16m'];
document.getElementById('sw-bands').innerHTML=SWB.map(b=>
  `<div class="swband${b==='31m'?' on':''}" data-b="${b}" onclick="swBand('${b}')">${b}</div>`).join('');
function swBand(b){SW_BAND=b;document.querySelectorAll('.swband').forEach(e=>
  e.classList.toggle('on', e.dataset.b===b));}
const _wfLast={am:0,sw:0};
function drawBandWF(dk,q){
  const cv=document.getElementById(dk+'-wf');
  if(!cv||!q.spec||!q.spec.length)return;
  cv.style.display='block';
  if(q.ts<=_wfLast[dk])return;          // one row per fresh chunk
  _wfLast[dk]=q.ts;
  const g=cv.getContext('2d');
  const img=g.getImageData(0,0,cv.width,cv.height);
  g.putImageData(img,0,6);              // scroll history down
  const row=g.createImageData(cv.width,6);
  const amber=v=>[Math.min(255,20+v*1.1),Math.min(255,8+v*0.75),4+v*0.12];
  const sepia=v=>[Math.min(255,14+v*1.0),Math.min(255,11+v*0.9),8+v*0.6];
  const pal=dk==='am'?amber:sepia;
  const sx=q.spec.length/cv.width;
  for(let x=0;x<cv.width;x++){
    const v=q.spec[Math.floor(x*sx)]||0;
    const [r,gg,b]=pal(v);
    for(let y=0;y<6;y++){
      const o=(y*cv.width+x)*4;
      row.data[o]=r;row.data[o+1]=gg;row.data[o+2]=b;row.data[o+3]=255;}}
  g.putImageData(row,0,0);
}
const _scanLast={am:0,sw:0};
function renderProgress(B){
  for(const dk of ['am','sw']){
    const bar=document.getElementById(dk+'-pbar');
    if(!bar)continue;
    if(B.scanning&&B.scan_pct!=null){
      bar.style.display='block';
      bar.firstElementChild.style.width=B.scan_pct+'%';
      bar.title='~'+B.scan_left+'s left';
    } else bar.style.display='none';
  }
  // a completed scan paints its full-band spectrum into the waterfall
  const sp=B.scan_spec;
  if(sp&&sp.deck&&sp.ts>_scanLast[sp.deck]){
    _scanLast[sp.deck]=sp.ts;
    drawBandWF(sp.deck,{spec:sp.row,ts:sp.ts});
  }
}
let _listenT0=0;
function renderQuality(B){
  renderProgress(B);
  const q=B.quality;
  for(const [dk,id] of [['am','am-quality'],['sw','sw-quality']]){
    const el=document.getElementById(id);
    if(q&&q.deck===dk&&q.stage&&q.carrier_snr_db==null){
      // startup narration: show exactly what the listener is doing,
      // with a bar pacing the ~9 s to first audio
      if(!_listenT0)_listenT0=Date.now();
      const pct=Math.min(95,(Date.now()-_listenT0)/9000*100);
      el.style.display='';
      el.innerHTML=`▶ ${q.stage}<div style="height:6px;margin-top:4px;`+
        `border:1px solid currentColor;border-radius:3px;opacity:.7">`+
        `<div style="width:${pct}%;height:100%;background:currentColor"></div></div>`;
      continue;
    }
    if(q&&q.deck===dk){
      _listenT0=0;
      el.style.display='';
      el.innerHTML=`TRUTH DIAL · carrier <b>${q.carrier_snr_db} dB</b>`+
        ` · co-channel ${q.cochannel}`+
        ` · fades ${(q.fade_frac6*100).toFixed(0)}%`+
        ` · BW ${(q.cutoff_hz/1000).toFixed(1)} kHz`+
        (q.hets_hz&&q.hets_hz.length?` · het notched @ ${q.hets_hz.join(', ')} Hz`:'')+
        ` · sideband tilt ${q.tilt_db>0?'+':''}${q.tilt_db} dB`;
      drawBandWF(dk,q);
    } else el.style.display='none';
  }
}
function dxLog(dk){
  const box=document.getElementById(dk+'-dx');
  if(box.style.display==='block'){box.style.display='none';return;}
  box.style.display='block';
  box.innerHTML='reading the logbook…';
  fetch('/api/dx/summary').then(r=>r.json()).then(j=>{
    const hue=dk==='am'?'#ffcf87':'#e8dcb8';
    let h=`<b style="color:${hue}">📖 DX LOGBOOK — band openings by hour</b>`+
      `<div style="font-size:11px;margin:2px 0 6px 0;opacity:.7">avg carriers heard per scan, by local hour — fills in as you keep scanning</div>`;
    const keys=Object.keys(j.hours).filter(k=>k.startsWith(dk+':')).sort();
    if(!keys.length)h+='<div>no scans logged yet for this deck — every scan from now on is remembered</div>';
    for(const k of keys){
      const hs=j.hours[k];
      const mx=Math.max(1,...Object.values(hs));
      let cells='';
      for(let hr=0;hr<24;hr++){
        const v=hs[hr]||0;
        const a=v?0.25+0.75*(v/mx):0.06;
        cells+=`<td title="${hr}:00 — ${v||0} avg" style="width:10px;height:14px;`+
          `background:${hue};opacity:${a.toFixed(2)}"></td>`;}
      h+=`<table style="border-spacing:1px;display:inline-table;margin:2px 8px 2px 0">`+
        `<tr><td style="padding-right:6px;color:${hue}">${k.split(':')[1]}</td>${cells}</tr></table>`;}
    h+=`<div style="font-size:10px;opacity:.6;margin-top:2px">0h ─ hours (local) ─ 23h</div>`;
    const best=(j.best||[]).filter(b=>b.deck===dk);
    if(best.length){
      h+=`<div style="margin-top:8px"><b style="color:${hue}">best-ever catches</b></div>`;
      h+=best.map(b=>`<div>${b.khz.toFixed(0)} kHz · ${b.db.toFixed(0)} dB · ${b.id}</div>`).join('');}
    box.innerHTML=h;
  }).catch(()=>{box.innerHTML='logbook read failed';});
}
function pollBand(){
  if(CUR_DECK==='fm')return;
  fetch('/api/band').then(r=>r.json()).then(B=>{
    renderQuality(B);
    if(CUR_DECK==='am'){
      const liveAM=B.quality&&B.quality.deck==='am';
      document.getElementById('am-status').textContent=B.scanning?'scanning…':
        liveAM?('● LIVE '+(+B.khz).toFixed(0)+' kHz'):
        (B.am_stations.length? B.am_stations.length+' carriers':'');
      if(B.khz&&B.deck==='am')document.getElementById('am-freq').textContent=(+B.khz).toFixed(0)+' kHz';
      if(B.am_hd&&B.am_hd.stage){
        let h='HD-AM: '+B.am_hd.stage+(B.am_hd.verdict?' — '+B.am_hd.verdict:'');
        if(B.am_hd.log)h+='<br><span style="color:#a8865f;font-size:11px">'+B.am_hd.log.slice(0,4).join('<br>')+'</span>';
        document.getElementById('am-hdbox').innerHTML=h;}
      const rows=B.am_stations.map(s=>{
        const w=Math.min(90,Math.max(4,(s.db-10)*1.6));
        const lk=s.link?` <a href="${s.link}" target="_blank" title="tower location map (radio-locator)" style="color:#ffb457;text-decoration:none">🗺</a>`:'';
        return `<tr><td><b style="color:#ffcf87">${s.khz.toFixed(0)}</b></td>`+
        `<td><span class="ambar" style="width:${w}px"></span> ${s.db.toFixed(0)} dB</td>`+
        `<td style="color:${s.id&&s.id.includes('MA3')?'#ffd700':'#d8b285'}">${s.id||'—'}${lk}</td>`+
        `<td><button class="ambtn" onclick="bandListen('am',${s.khz})">▶ LISTEN</button></td>`+
        `<td><button class="ambtn" onclick="amHD(${s.khz})">HD?</button></td></tr>`;});
      if(rows.length)document.getElementById('am-rows').innerHTML=rows.join('');
    }
    if(CUR_DECK==='sw'){
      const liveSW=B.quality&&B.quality.deck==='sw';
      let swTxt=B.scanning?
        ('scanning '+B.sw_band+'… ('+B.sw_stations.length+' so far)'):
        liveSW?('● LIVE '+(+B.khz).toFixed(0)+' kHz'):
        (B.sw_stations.length? B.sw_stations.length+' carriers':'');
      if(!B.scanning&&!liveSW&&B.sw_age_s!=null&&B.sw_stations.length){
        const m=Math.round(B.sw_age_s/60);
        swTxt+=' · scanned '+(m<60?m+' min':Math.round(m/60)+' h')+' ago';
        if(B.sw_age_s>2400)swTxt+=' — schedules turn over on UTC hour marks, rescan';
      }
      document.getElementById('sw-status').textContent=swTxt;
      if(B.scanning&&B.sw_band){SW_BAND=B.sw_band;
        document.querySelectorAll('.swband').forEach(e=>
          e.classList.toggle('on', e.dataset.b===B.sw_band));}
      const rows=B.sw_stations.map(s=>{
        const w=Math.min(90,Math.max(4,(s.db-8)*2));
        const lk=s.link?` <a href="${s.link}" target="_blank" title="short-wave.info" style="color:#f0e4c0;text-decoration:none">🔗</a>`:'';
        const bd=s.band?`<span style="color:#8a7f60;font-size:10px"> ${s.band}</span>`:'';
        return `<tr><td><b style="color:#e8dcb8">${s.khz.toFixed(0)}</b>${bd}</td>`+
        `<td><span class="swbar" style="width:${w}px"></span> ${s.db.toFixed(0)} dB</td>`+
        `<td style="color:#cfc4a0">${s.id||'<span style="color:#8a7f60">unlisted</span>'}${lk}`+
        (s.tx?`<br><span style="color:#9a8a5f;font-size:11px">📡 TX: ${s.tx}</span>`:'')+`</td>`+
        `<td><button class="swbtn" onclick="bandListen('sw',${s.khz})">▶ LISTEN</button> `+
        `<button class="swbtn" onclick="swSched(${s.khz})" title="when to listen — full day schedule">📅</button></td></tr>`;});
      if(rows.length)document.getElementById('sw-rows').innerHTML=rows.join('');
    }
  }).catch(()=>{});
}
setInterval(pollBand, 2500);
if(localStorage.getItem('rt_deck'))deck(localStorage.getItem('rt_deck'));
</script><script>
let stations=[];
// ── live spectrum + waterfall ──────────────────────────────────────
let specT=0,specLo=null,specHi=null;
const wfPal=(()=>{const p=[];for(let i=0;i<256;i++){const v=i/255;
let r,g,b;if(v<.35){r=3+v*40;g=8+v*90;b=20+v*380}
else if(v<.7){const u=(v-.35)/.35;r=17+u*0;g=40+u*189;b=153+u*102}
else{const u=(v-.7)/.3;r=0+u*255;g=229-u*186;b=255-u*41}
p.push([Math.min(255,r|0),Math.min(255,g|0),Math.min(255,b|0)])}
return p})();
// waterfall history lives in an offscreen buffer; overlays (channel
// lines, scan cursor) are composited onto the visible canvas each
// frame so a moving cursor never burns ghost trails into the history
const wfBuf=document.createElement('canvas');
wfBuf.width=688;wfBuf.height=150;
let lastDb=null;
function drawLineGraph(db){
const line=document.getElementById('specline'),lg=line.getContext('2d');
const n=db.length,rng=Math.max((specHi||0)-(specLo||0),10);
lg.fillStyle='#02040a';lg.fillRect(0,0,line.width,line.height);
if(specMode!=='band'){lg.strokeStyle='rgba(255,43,214,.45)';
lg.beginPath();lg.moveTo(line.width/2,0);
lg.lineTo(line.width/2,line.height);lg.stroke()}
lg.strokeStyle='#00e5ff';lg.shadowColor='#00e5ff';lg.shadowBlur=7;
lg.beginPath();
for(let x=0;x<line.width;x++){const bin=Math.floor(x*n/line.width);
const v=Math.max(0,Math.min(1,(db[bin]-specLo)/rng));
const y=line.height-3-v*(line.height-8);
if(x===0)lg.moveTo(x,y);else lg.lineTo(x,y)}
lg.stroke();lg.shadowBlur=0}
function drawSpecRows(rows){
const bg=wfBuf.getContext('2d');
for(const row of rows){const db=row.db,n=db.length;
let lo=Infinity,hi=-Infinity;
for(const v of db){if(v<lo)lo=v;if(v>hi)hi=v}
specLo=specLo===null?lo:specLo*.95+lo*.05;
specHi=specHi===null?hi:specHi*.95+hi*.05;
const rng=Math.max(specHi-specLo,10);
bg.drawImage(wfBuf,0,0,wfBuf.width,wfBuf.height-1,
0,1,wfBuf.width,wfBuf.height-1);
const img=bg.createImageData(wfBuf.width,1);
for(let x=0;x<wfBuf.width;x++){const bin=Math.floor(x*n/wfBuf.width);
const v=Math.max(0,Math.min(1,(db[bin]-specLo)/rng));
const c=wfPal[(v*255)|0];const o=x*4;
img.data[o]=c[0];img.data[o+1]=c[1];img.data[o+2]=c[2];
img.data[o+3]=255}
bg.putImageData(img,0,0)}
lastDb=rows[rows.length-1].db;
drawLineGraph(lastDb)}
function blitWf(){const wf=document.getElementById('wfall');
wf.getContext('2d').drawImage(wfBuf,0,0)}
let specMode=null,specCursor=null,specScanning=false;
function bandX(mhz,w){return (mhz-88.0)/20.0*w}
function drawWfOverlay(){
// channel guide lines ON the waterfall: every station a faint
// vertical line (cyan = HD), redrawn each poll so they ride on top
// of the scrolling history; the cursor is the bright one
const wf=document.getElementById('wfall'),wg=wf.getContext('2d');
for(const s of stations){const x=bandX(s.mhz,wf.width);
wg.strokeStyle=s.hd?'rgba(0,229,255,.28)':'rgba(90,140,160,.18)';
wg.beginPath();wg.moveTo(x,0);wg.lineTo(x,wf.height);wg.stroke()}
if(specCursor!=null){const x=bandX(specCursor,wf.width);
wg.strokeStyle=specScanning?'rgba(255,43,214,.95)':'rgba(0,229,255,.8)';
wg.lineWidth=2;wg.shadowColor=specScanning?'#ff2bd6':'#00e5ff';
wg.shadowBlur=8;wg.beginPath();wg.moveTo(x,0);wg.lineTo(x,wf.height);
wg.stroke();wg.lineWidth=1;wg.shadowBlur=0}}
function drawBandMarks(lg,w,h){
// channel ruler: a tick each MHz, label every 4; station marks from
// the guide; HD stations get magenta sideband shading (the +-129 to
// +-198 kHz shelves where ALL the subchannels HD1-HD3 actually live)
lg.strokeStyle='#2a5a6e';lg.fillStyle='#6fb0c2';lg.font='10px Consolas';
for(let m=88;m<=108;m++){const x=bandX(m,w);lg.beginPath();
lg.moveTo(x,0);lg.lineTo(x,m%4===0?14:7);lg.stroke();
if(m%4===0)lg.fillText(m,x+2,11)}
if(specCursor!=null){const x=bandX(specCursor,w);
lg.strokeStyle=specScanning?'#ff2bd6':'#00e5ff';lg.lineWidth=2;
lg.shadowColor=specScanning?'#ff2bd6':'#00e5ff';lg.shadowBlur=8;
lg.beginPath();lg.moveTo(x,0);lg.lineTo(x,h);lg.stroke();
lg.lineWidth=1;lg.shadowBlur=0}
for(const s of stations){const x=bandX(s.mhz,w);
if(s.hd){const x1=bandX(s.mhz-0.198,w),x2=bandX(s.mhz-0.129,w),
x3=bandX(s.mhz+0.129,w),x4=bandX(s.mhz+0.198,w);
lg.fillStyle='rgba(255,43,214,.22)';
lg.fillRect(x1,0,x2-x1,h);lg.fillRect(x3,0,x4-x3,h);
const np_=Object.keys(s.programs||{}).length||1;
lg.fillStyle='#ff8fe8';lg.font='9px Consolas';
lg.fillText('HD'+(np_>1?'×'+np_:''),x-9,h-3)}
lg.strokeStyle=s.hd?'rgba(0,229,255,.8)':'rgba(51,86,106,.8)';
lg.beginPath();lg.moveTo(x,0);lg.lineTo(x,h);lg.stroke()}}
async function specPoll(){try{
const r=await(await fetch('/api/spectrum?since='+specT)).json();
const rows=r.rows||[];
const box=document.getElementById('specbox');
if(r.mode!==specMode){specMode=r.mode;specT=0;specLo=specHi=null;
lastDb=null;
const wf=document.getElementById('wfall');
wf.getContext('2d').clearRect(0,0,wf.width,wf.height);
wfBuf.getContext('2d').clearRect(0,0,wfBuf.width,wfBuf.height);
document.getElementById('speclabel').textContent=
r.mode==='band'?'FULL FM BAND · 88–108 MHz · idle sweep':
'LIVE SPECTRUM · ±250 kHz AROUND THE DIAL'}
specCursor=r.cursor_mhz;specScanning=!!r.scanning;
if(rows.length){specT=rows[rows.length-1].t;
box.style.display='block';drawSpecRows(rows)}
if(box.style.display==='block'){
if(r.mode==='band'){
if(!rows.length&&lastDb)drawLineGraph(lastDb);
blitWf();
const line=document.getElementById('specline');
drawBandMarks(line.getContext('2d'),line.width,line.height);
drawWfOverlay()}
else if(rows.length)blitWf()}
}catch(e){}
setTimeout(specPoll,350)}
specPoll();
async function survey(){document.getElementById('status').textContent=
'surveying: sweeps the band, probes each strong station for HD (~4 min)';
await fetch('/api/survey',{method:'POST'})}
async function stopL(){await fetch('/api/stop',{method:'POST'})}
let castOn=false;
async function castToggle(dk){
const el=document.getElementById(dk==='am'?'am-status':dk==='sw'?'sw-status':'status');
el.textContent=castOn?
'stopping speaker cast...':'starting the wi-fi speaker stream...';
await fetch('/api/cast',{method:'POST',
body:JSON.stringify({on:!castOn,deck:dk||'fm'})});
if(dk&&dk!=='fm'){castOn=!castOn;setTimeout(()=>{el.textContent=castOn?'casting to speakers':'';},8000)}}
function antSel(){return document.getElementById('antsel').value}
async function listenFM(mhz,name){
document.getElementById('status').textContent='tuning '+mhz.toFixed(1)+
' analog (stereo v2) - audio in ~4 s';
await fetch('/api/listen_fm',{method:'POST',
body:JSON.stringify({mhz,name,antenna:antSel()})})}
async function listen(mhz,prog,name){
document.getElementById('status').textContent='tuning '+mhz.toFixed(1)+
' program '+prog+' - audio in ~8-12 s';
await fetch('/api/listen',{method:'POST',
body:JSON.stringify({mhz,prog,name,antenna:antSel()})})}
function drawDial(cur){
const c=document.getElementById('dialc'),g=c.getContext('2d');
g.fillStyle='#02040a';g.fillRect(0,0,c.width,c.height);
const x=m=>((m-87.5)/(108.3-87.5))*c.width;
g.strokeStyle='#113a4a';g.fillStyle='#4a8a9a';
g.font='16px Consolas';
for(let m=88;m<=108;m+=2){g.beginPath();
g.moveTo(x(m),0);g.lineTo(x(m),22);g.stroke();
g.fillText(m,x(m)-12,44)}
for(const s of stations){const px=x(s.mhz);
g.fillStyle=s.hd?'#00e5ff':'#33566a';
g.shadowColor=s.hd?'#00e5ff':'transparent';g.shadowBlur=s.hd?10:0;
g.beginPath();g.arc(px,78,s.hd?7:4,0,7);g.fill();g.shadowBlur=0}
if(cur){g.strokeStyle='#ff2bd6';g.lineWidth=3;g.shadowColor='#ff2bd6';
g.shadowBlur=12;g.beginPath();
g.moveTo(x(cur),0);g.lineTo(x(cur),c.height);g.stroke();
g.lineWidth=1;g.shadowBlur=0}}
function ncard(k,v,bar){return '<div class="ncard"><div class="k">'+k+
'</div><div class="v">'+v+'</div>'+(bar!=null?
'<div class="nbar"><div style="width:'+
Math.max(0,Math.min(100,bar))+'%"></div></div>':'')+'</div>'}
async function refresh(){try{
const s=await (await fetch('/api/state')).json();
stations=s.stations||[];
document.getElementById('freq').textContent=
s.mhz?s.mhz.toFixed(1)+' FM':'\\u2014 \\u00b7 \\u2014';
if(s.listening){
document.getElementById('nowplaying').lastElementChild.innerHTML=
'<span class="t">'+(s.title||s.name||'')+'</span><br><span class="a">'+
(s.artist||'')+(s.album?' &mdash; '+s.album:'')+'</span>'+
(s.message?'<br><span class="rssi">'+s.message+'</span>':'');
const art=document.getElementById('art');
const key=(s.title||'')+(s.artist||'');
if(s.prog!=null&&key!==art.dataset.k){art.dataset.k=key;
art.src='/api/art?'+Date.now();art.onload=()=>art.style.display='';
art.onerror=()=>art.style.display='none';}
if(s.prog==null){art.style.display='none';}}
const ab=document.getElementById('alertbar');
if(s.alert){ab.style.display='';ab.textContent=
'\\u26a0 EMERGENCY ALERT: '+s.alert;}
else{ab.style.display='none';}
// HD QUALITY: MER vs the measured cliff. >=13 solid, 11-13 will
// stutter, <9.5 will not hold (the FM button is the better ear).
const hb=document.getElementById('hdqbox');
if(s.prog!=null&&s.mer_lo!=null){
const mer=(Number(s.mer_lo)+Number(s.mer_hi||s.mer_lo))/2;
const q=Math.max(0,Math.min(100,(mer-8)/6*100));
const lbl=mer>=13?'SOLID':mer>=11?'OK':mer>=9.5?'FRAGILE':'TOO WEAK';
const col=mer>=13?'#39ff8a':mer>=11?'#ffb84d':'#ff6b4d';
hb.style.display='';
document.getElementById('hdq').textContent=lbl;
document.getElementById('hdq').style.color=col;
const qb=document.getElementById('hdqbar');
qb.style.width=q+'%';qb.style.background=col;
if(mer<11&&s.pct===100)document.getElementById('status').textContent=
'HD is fragile here (MER '+mer.toFixed(1)+') \\u2014 expect dropouts;'+
' the FM button will sound cleaner';
}else{hb.style.display='none';}
document.getElementById('mlo').textContent=s.mer_lo??'\\u2014';
document.getElementById('mhi').textContent=s.mer_hi??'\\u2014';
document.getElementById('ber').textContent=s.ber!=null?s.ber.toFixed(4):'\\u2014';
document.getElementById('lock').textContent=s.sync?'\\u25cf':'\\u2014';
document.getElementById('lock').style.color=s.sync?'#39ff8a':'#3f6a78';
const au=document.getElementById('audio');
au.textContent=s.audio==='MUSIC/SPEECH'?'\\u266a':(s.audio==='STATIC'?'\\u2717':
(s.audio==='SILENCE'?'\\u2026':(s.audio||'\\u2014')));
au.style.color=s.audio==='MUSIC/SPEECH'?'#39ff8a':
(s.audio==='STATIC'?'#ff3b3b':'#3f6a78');
const pb=document.getElementById('pbar');
if(s.survey&&s.survey.running){
document.getElementById('status').textContent=
'[SCAN] '+s.survey.line+' ('+s.survey.pct+'%)';
pb.style.display='block';
pb.firstElementChild.style.width=(s.survey.pct||2)+'%';}
else if(s.stage&&s.pct<100){document.getElementById('status').textContent=
(s.pct===0?'[!] ':'[~] ')+s.stage;
pb.style.display='block';pb.firstElementChild.style.width=(s.pct||2)+'%';}
else{pb.style.display='none';
if(s.listening&&s.pct===100)document.getElementById('status').textContent='';}
drawDial(s.mhz);
let ng=ncard('DECODER',s.decoder||'idle');
if(s.antenna)ng+=ncard('ANTENNA (auto)',s.antenna+' ['+
(s.hour_band||'?')+' table]');
if(s.ifgr!=null)ng+=ncard('GAIN IN USE','IFGR '+s.ifgr+' / RF '+s.rfgain);
ng+=ncard('RADIO LOCK',s.lock?(s.lock.owner+': '+
(s.lock.purpose||'')):'free');
const cb=document.getElementById('castbtn');
if(s.cast){cb.style.display='';castOn=!!s.cast.on;
cb.innerHTML=castOn?'&#x23F9; STOP CAST':'&#x1F50A; CAST TO WI-FI SPEAKERS';
if(castOn)ng+=ncard('CAST',(s.cast.zones||[]).join(', ')||'on');
else if(s.cast.err)ng+=ncard('CAST',s.cast.err);}
else{cb.style.display='none';}
if(s.pilot_snr_db!=null){
ng+=ncard('19K PILOT SNR',s.pilot_snr_db+' dB',s.pilot_snr_db/40*100);
ng+=ncard('AUDIO SNR',s.audio_snr_db+' dB',s.audio_snr_db/50*100);
ng+=ncard('STEREO BLEND',Math.round((s.stereo_blend||0)*100)+'% '+
(s.fm_mode||''),(s.stereo_blend||0)*100);
ng+=ncard('AGC',(s.agc_db>0?'+':'')+s.agc_db+' dB');}
if(s.mer_lo!=null)ng+=ncard('MER LO/HI',s.mer_lo+' / '+(s.mer_hi??'?')+
' dB',s.mer_lo/16*100);
if(s.ber!=null)ng+=ncard('BER',s.ber.toFixed(4),
100-Math.min(100,s.ber*2000));
if(s.audio)ng+=ncard('AUDIO VERDICT',s.audio);
if(s.genre)ng+=ncard('GENRE',s.genre);
if(s.tower)ng+=ncard('TOWER LOCATION',s.tower);
document.getElementById('nerdgrid').innerHTML=ng;
document.getElementById('daylab').textContent=
s.daylab?('DAY LAB \\u25b8 '+s.daylab):'DAY LAB \\u25b8 idle';
function grade(st,t){
// measured listening-quality forecast, not vibes:
// HD from the tune table's referee audio-seconds, FM from pilot SNR
let hdg=null,fmg=null,ant='';
if(t){const he=t.hd_evidence||{},fe=t.fm_evidence||{};
if(t.hd_ant){hdg=he.aud>=10?'A':he.aud>0?'B':'C';}
else if(st.hd){hdg='C';}
fmg=(fe.pilot||-99)>=25?'A':(fe.pilot||-99)>=15?'B':
(fe.pilot||-99)>6?'C':null;
ant=(t.hd_ant||t.fm_ant||'').replace('Antenna ','');}
else if(st.hd){hdg=st.mer_lo>=10?'A':st.mer_lo>=4?'B':'C';}
return {hdg,fmg,ant};}
const GCOL={A:'#39ff8a',B:'#ffb84d',C:'#ff6b4d'};
let h='<table>';
for(const st of stations){
const t=(s.tune||{})[st.mhz.toFixed(1)];
const g=grade(st,t);
const w=Math.max(4,Math.min(100,st.rssi/40*100));
const isLive=s.listening&&s.mhz===st.mhz;
h+='<tr'+(isLive?' style="background:rgba(255,43,214,.07)"':'')+
'><td style="width:40px">'+
(st.logo?'<img src="/api/logo?mhz='+st.mhz.toFixed(1)+
'" style="width:34px;height:34px;border-radius:4px" '+
'onerror="this.style.display=\\'none\\'">':'')+'</td>'+
'<td class="st" style="min-width:230px">'+st.mhz.toFixed(1)+
' '+(st.name||'')+(st.hd?'<span class="hd">HD</span>':'')+
(st.genre?' <span class="rssi">'+st.genre+'</span>':'')+
(g.ant?' <span class="rssi">&#x25B8; ant '+g.ant+'</span>':'')+
(isLive?'<br><span style="color:#ff2bd6">&#x25B6; NOW: '+
(s.title||'')+(s.artist?' &mdash; '+s.artist:'')+'</span>':'')+
'<div style="height:5px;margin-top:3px;background:#02040a;'+
'border-radius:3px;overflow:hidden;max-width:220px">'+
'<div style="height:100%;width:'+w+'%;background:linear-gradient('+
'90deg,#00e5ff,'+(g.hdg?GCOL[g.hdg]:'#3f6a78')+')"></div></div></td><td>';
if(st.hd){const progs=Object.keys(st.programs||{}).length?
Object.entries(st.programs):[["0","HD1"]];
for(const [p,label] of progs){h+='<button class="prog" onclick="listen('+
st.mhz+','+p+',\\''+(st.name||st.mhz)+'\\')">HD'+(parseInt(p)+1)+
' <span style="color:#3f6a78;font-size:10px">'+label+'</span></button>'}}
h+='<button class="prog" style="border-color:#39ff8a" onclick="listenFM('+
st.mhz+',\\''+(st.name||st.mhz)+'\\')">FM</button>';
h+='</td><td class="rssi" style="min-width:130px">+'+st.rssi+' dB'+
(g.hdg?' | HD <b style="color:'+GCOL[g.hdg]+'">'+g.hdg+'</b>':'')+
(g.fmg?' | FM <b style="color:'+GCOL[g.fmg]+'">'+g.fmg+'</b>':'')+
'</td></tr>'}
document.getElementById('guide').innerHTML=h+'</table>';
}catch(e){}}
setInterval(refresh,1500);refresh();
</script></body></html>"""


_DAYLAB = {"t": 0.0, "line": ""}
_TUNE_CACHE = {"t": 0.0, "d": {}}


def daylab_line():
    """Last line of the all-day lab's log (cached 5 s) so the nerd tab
    shows what the background science is doing right now."""
    now = time.time()
    if now - _DAYLAB["t"] > 5:
        try:
            txt = Path(r"Z:\SDR_Agent_v2\hd_day_lab_log.txt").read_text()
            _DAYLAB["line"] = txt.strip().splitlines()[-1]
        except Exception:
            _DAYLAB["line"] = ""
        _DAYLAB["t"] = now
    return _DAYLAB["line"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/api/logo"):
            # station logo banked by the survey: /api/logo?mhz=93.3
            try:
                mhz = self.path.split("mhz=")[1].split("&")[0]
                st = json.loads(STATIONS.read_text(encoding="utf-8"))
                ent = next(s for s in st["stations"]
                           if f"{s['mhz']:.1f}" == mhz)
                p = LAB / "aas_guide" / mhz / ent["logo"]
                ctype = ("image/png" if p.suffix == ".png"
                         else "image/jpeg")
                self._send(p.read_bytes(), ctype)
            except Exception:
                self.send_error(404)
        elif self.path.startswith("/api/art"):
            # newest image the station pushed (album art / logo LOTs)
            imgs = sorted(
                (LAB / "aas").glob("*.jp*g"), key=lambda p:
                p.stat().st_mtime, reverse=True) + sorted(
                (LAB / "aas").glob("*.png"), key=lambda p:
                p.stat().st_mtime, reverse=True)
            imgs = sorted(imgs, key=lambda p: p.stat().st_mtime,
                          reverse=True)
            if imgs:
                ctype = ("image/png" if imgs[0].suffix == ".png"
                         else "image/jpeg")
                self._send(imgs[0].read_bytes(), ctype)
            else:
                self.send_error(404)
        elif self.path.startswith("/api/spectrum"):
            since = 0.0
            if "since=" in self.path:
                try:
                    since = float(self.path.split("since=")[1]
                                  .split("&")[0])
                except ValueError:
                    pass
            listening = bool(STATE.get("listening"))
            src = SPEC["rows"] if listening else BAND["rows"]
            rows = [r for r in list(src) if r["t"] > since]
            cursor = (SURVEY.get("cur_mhz") if SURVEY.get("running")
                      else STATE.get("last_mhz"))
            self._send(json.dumps(
                {"mode": "station" if listening else "band",
                 "mhz": STATE.get("mhz"),
                 "span_hz": 2 * SPEC_SPAN,
                 "lo_hz": BAND_LO, "hi_hz": BAND_HI,
                 "listening": listening,
                 "scanning": bool(SURVEY.get("running")),
                 "cursor_mhz": cursor,
                 "rows": rows[-30:]}))
        elif self.path == "/api/band":
            b = {k: BAND.get(k) for k in
                 ("am_stations", "sw_stations", "sw_band", "am_hd",
                  "deck", "khz", "scanning", "scan_spec")}
            for dk in ("am", "sw"):    # grid staleness: SW turns over at
                try:                   # UTC hour marks — say how old it is
                    b[dk + "_age_s"] = int(
                        time.time() - (LAB / f"{dk}_stations.json").stat().st_mtime)
                except OSError:
                    pass
            if BAND.get("scanning") and BAND.get("scan_t0"):
                el = time.time() - BAND["scan_t0"]
                eta = max(1.0, BAND.get("scan_eta", 25))
                b["scan_pct"] = min(96, int(100 * el / eta))
                b["scan_left"] = max(0, int(eta - el))
            try:
                q = json.loads((LAB / "band_quality.json").read_text())
                # live loops publish every ~6 s; 20 s = gone means gone
                if time.time() - q.get("ts", 0) < 20:
                    b["quality"] = q
            except (OSError, ValueError):
                pass
            self._send(json.dumps(b))
            return
        elif self.path == "/api/dx/summary":
            self._send(json.dumps(dx_summary()))
            return
        elif self.path.startswith("/api/sw/sched"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            khz = float(q.get("khz", ["0"])[0])
            self._send(json.dumps({"khz": khz, "sched": sw_schedule(khz)}))
            return
        elif self.path == "/api/state":
            st = dict(STATE)
            st["survey"] = dict(SURVEY)
            st["daylab"] = daylab_line()
            h = time.gmtime().tm_hour
            st["hour_band"] = "day" if 11 <= h < 19 else "evening"
            try:
                import cast_local
                st["cast"] = cast_local.status()
            except Exception:
                st["cast"] = None
            # per-station measured quality (the tune-table's evidence)
            now = time.time()
            if now - _TUNE_CACHE["t"] > 5:
                try:
                    _TUNE_CACHE["d"] = json.loads(
                        (LAB / "radio_tune_table.json").read_text())
                except Exception:
                    _TUNE_CACHE["d"] = {}
                _TUNE_CACHE["t"] = now
            st["tune"] = (_TUNE_CACHE["d"] or {}).get("stations", {})
            try:
                import radio_lock
                st["lock"] = radio_lock.status()
            except Exception:
                st["lock"] = None
            try:
                st["stations"] = json.loads(
                    STATIONS.read_text(encoding="utf-8"))["stations"]
            except (OSError, json.JSONDecodeError, KeyError):
                st["stations"] = []
            self._send(json.dumps(st))
        else:
            self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/survey":
            threading.Thread(target=run_survey, daemon=True).start()
            self._send('"surveying"')
        elif self.path == "/api/listen_fm":
            threading.Thread(target=listen_fm,
                             args=(req["mhz"], req.get("name", ""),
                                   req.get("ifgr", 59),
                                   req.get("rfgain", "3"),
                                   ANT_PICK.get(req.get("antenna",
                                                        "auto"))),
                             daemon=True).start()
            self._send('"listening analog"')
        elif self.path == "/api/listen":
            threading.Thread(target=listen,
                             args=(req["mhz"], req["prog"],
                                   req.get("name", ""),
                                   req.get("ifgr", 59),
                                   req.get("rfgain", "3"),
                                   ANT_PICK.get(req.get("antenna",
                                                        "auto"))),
                             daemon=True).start()
            self._send('"listening"')
        elif self.path == "/api/am/scan":
            if BAND.get("scanning"):
                self._send('"busy"')
                return
            threading.Thread(target=am_scan, daemon=True).start()
            self._send('"scanning"')
        elif self.path == "/api/sw/scan":
            if BAND.get("scanning"):
                self._send('"busy"')
                return
            threading.Thread(target=sw_scan,
                             args=(req.get("band", "31m"),),
                             daemon=True).start()
            self._send('"scanning"')
        elif self.path == "/api/sw/scan_all":
            if BAND.get("scanning"):
                self._send('"busy"')
                return
            threading.Thread(target=sw_scan_all, daemon=True).start()
            self._send('"scanning"')
        elif self.path == "/api/band/listen":
            threading.Thread(target=band_listen,
                             args=(req.get("deck", "am"),
                                   float(req.get("khz", 820))),
                             daemon=True).start()
            self._send('"listening"')
        elif self.path == "/api/am/hd":
            am_hd_try(float(req.get("khz", 820)))
            self._send('"trying"')
        elif self.path == "/api/stop":
            BAND["hold"] = False        # un-bench the idle sweeper
            threading.Thread(target=stop_listen, daemon=True).start()
            try:
                import cast_local
                threading.Thread(target=cast_local.stop,
                                 daemon=True).start()
            except Exception:
                pass
            self._send('"stopped"')
        elif self.path == "/api/cast":
            def do_cast():
                import cast_local
                if req.get("on"):
                    # deck-aware source: FM casts the live decode WAV;
                    # AM casts the growing sync-AM raw; SW the last catch
                    deck = req.get("deck") or BAND.get("deck") or "fm"
                    src = None
                    name = STATE.get("name") or "radio"
                    if deck in ("am", "sw") and BAND.get("khz"):
                        src = str(LAB / "band_live.s16")
                        name = f"{deck.upper()} {BAND['khz']:.0f} kHz"
                    try:
                        st = cast_local.start(f"RADIO TUNA - {name}",
                                              source=src)
                    except TypeError:      # older private cast module
                        st = cast_local.start(f"RADIO TUNA - {name}")
                    if st.get("on"):
                        # the house runs ~15 s behind the burst buffer;
                        # two copies at an offset is an echo chamber —
                        # the PC yields to the whole-house stream
                        PLAYER["mpv"] = None
                        subprocess.run(["taskkill", "/F", "/IM",
                                        "mpv.exe"], capture_output=True)
                else:
                    cast_local.stop()
                    # bring local audio back if a station is playing
                    if STATE.get("listening") \
                            and STATE.get("prog") is not None:
                        # HD session: reattach the speakers to the live
                        # audio pipe (never tail the file — ear-static)
                        m = subprocess.Popen(
                            [MPV] + MPV_PIPE_ARGS
                            + [f"--title=ALBACORE TUNA - "
                               f"{STATE.get('name') or ''}"],
                            stdin=subprocess.PIPE)
                        LIVE_PROCS.append(m)
                        PLAYER["mpv"] = m
                        return
                    wav = LAB / (STATE.get("wav") or "radio_live.wav")
                    if STATE.get("listening") and wav.exists():
                        mpv = subprocess.Popen(
                            [MPV, str(wav), "--volume=100",
                             "--keep-open=yes", "--force-seekable=yes",
                             "--start=100%",
                             f"--title=ALBACORE TUNA - "
                             f"{STATE.get('name') or ''}"])
                        LIVE_PROCS.append(mpv)
            threading.Thread(target=do_cast, daemon=True).start()
            self._send('"casting"')
        else:
            self.send_error(404)


if __name__ == "__main__":
    print(f"Radio Tuna panel: http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer((os.environ.get("RADIO_PANEL_BIND", "127.0.0.1"), PORT), H).serve_forever()
