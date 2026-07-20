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
  LISTEN â€” click a subchannel: SDR pump -> nrsc5 -> growing WAV -> mpv,
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
         "antenna": None, "ifgr": None, "rfgain": None}

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
        STATE.update({"listening": False, "mhz": None, "prog": None,
                      "name": None, "sync": False, "stage": "", "pct": 0,
                      "decoder": None, "antenna": None, "ifgr": None,
                      "rfgain": None})
        STATE.update({k: None for k in FM_KEYS})
    for p in LIVE_PROCS:
        try:
            p.terminate()
        except Exception:
            pass
    LIVE_PROCS.clear()
    subprocess.run(["taskkill", "/F", "/IM", "mpv.exe"], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "nrsc5.exe"],
                   capture_output=True)


# â”€â”€ band survey â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def fm_power_sweep():
    """Wideband FFT hops across 88-108; returns {mhz: rssi_db} at the
    odd-tenth US channel frequencies."""
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    hops = [91.0, 97.0, 103.0]          # 8 MS/s each covers ~7 MHz well
    found = {}
    for hop in hops:
        sdr, st = open_sdr(hop, ifgr=59, rfgain="3", rate=8_000_000)
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
    keeper = subprocess.Popen(["powershell", "-NoProfile", "-Command",
                               "Start-Sleep -Seconds 90"],
                              stdout=subprocess.PIPE)
    p = subprocess.Popen([NRSC5, "-r", str(iq), str(0)],
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


def run_survey():
    if SURVEY["running"]:
        return
    SURVEY.update({"running": True, "pct": 2,
                   "line": "sweeping the bandâ€¦"})
    try:
        stop_listen()
        time.sleep(1)
        carriers = fm_power_sweep()
        strong = {f: v for f, v in carriers.items() if v >= 14}
        SURVEY.update({"pct": 15,
                       "line": f"{len(strong)} strong stations â€” "
                               "probing for HDâ€¦"})
        stations = []
        done = 0
        for mhz, rssi in sorted(strong.items()):
            SURVEY["line"] = f"probing {mhz:.1f} MHzâ€¦"
            info = hd_probe(mhz)
            done += 1
            SURVEY["pct"] = 15 + int(80 * done / max(1, len(strong)))
            stations.append({"mhz": mhz, "rssi": rssi,
                             "hd": info.get("hd", False),
                             "name": info.get("name"),
                             "slogan": info.get("slogan"),
                             "programs": info.get("programs", {}),
                             "mer_lo": info.get("mer_lo"),
                             "mer_hi": info.get("mer_hi"),
                             "ber": info.get("ber")})
            STATIONS.write_text(json.dumps(
                {"surveyed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "stations": stations}, indent=1), encoding="utf-8")
        SURVEY.update({"pct": 100, "line": "survey complete"})
    except Exception as e:
        SURVEY["line"] = f"survey failed: {e}"
    finally:
        SURVEY["running"] = False


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


def listen(mhz, prog, name, ifgr=59, rfgain="3"):
    ifgr, rfgain = _cal_gains(mhz, ifgr, rfgain)
    STATE["ifgr"], STATE["rfgain"] = ifgr, str(rfgain)
    stop_listen()
    time.sleep(1)
    with LOCK:
        GEN[0] += 1
        my_gen = GEN[0]
        STATE.update({"mhz": mhz, "prog": prog, "name": name,
                      "listening": True, "sync": False,
                      "title": None, "artist": None})

    set_stage(8, "warming the tubes â€” opening the radio")

    def worker():
        sdr = st = None
        ant = pick_antenna(mhz, "hd")
        STATE["antenna"] = ANT_NICK.get(ant, ant)
        for attempt in range(4):          # post-restart contention retry
            try:
                sdr, st = open_sdr(mhz, ifgr=ifgr, rfgain=str(rfgain),
                                   ant=ant)
                break
            except Exception:
                if GEN[0] != my_gen:
                    return
                set_stage(8, f"radio busy â€” retrying ({attempt + 2}/4)")
                time.sleep(2.5)
        if sdr is None:
            set_stage(0, "RADIO UNAVAILABLE â€” another process holds the "
                         "SDR; stop it and click again")
            STATE.update({"listening": False})
            return
        set_stage(30, "receiving â€” streaming into the HD decoder")
        wav = LAB / "radio_live.wav"
        try:
            wav.unlink()
        except OSError:
            pass
        buf = np.empty(2 * 65536, np.int16)
        # STREAMING (2026-07-05): nrsc5 -r - reads IQ from stdin, so the
        # radio pumps straight into the decoder â€” no growing-file EOF
        # stall (this build stops at EOF instead of tailing).
        nr = subprocess.Popen(
            [NRSC5, "-r", "-", "-o", str(wav), str(prog)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=False, env=_nrsc5_env())
        LIVE_PROCS.append(nr)
        STATE["decoder"] = DECODER_TAG
        info = {}

        def scrape():
            for raw_line in nr.stdout:
                try:
                    line = raw_line.decode("utf-8", "replace")
                except AttributeError:
                    line = raw_line
                parse_nrsc5_line(line, info)
                for k in ("title", "artist", "mer_lo", "mer_hi",
                          "ber", "sync"):
                    if k in info:
                        STATE[k] = info[k]
        threading.Thread(target=scrape, daemon=True).start()
        set_stage(45, "decoder hunting sync")
        nr_t0 = time.time()
        mpv = None
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
        while GEN[0] == my_gen:
            r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
            if r.ret > 0:
                n = r.ret - (r.ret & 1)      # keep I/Q pairing even
                try:
                    q.put_nowait(buf[:2 * n].copy())
                except queue.Full:
                    pass                     # decoder hopeless behind; skip
            if STATE.get("sync") and STATE["pct"] < 70:
                set_stage(70, "SYNC â€” decoding digital audio")
            # honesty + rescue: no sync in 25 s = this station's HD is out
            # of reach here â€” fall back to analog so a click ends in sound
            if not STATE.get("sync") and time.time() - nr_t0 > 25:
                set_stage(30, "no HD sync â€” digital too weak here; "
                              "switching to analog FMâ€¦")
                close_sdr(sdr, st)
                try:
                    nr.terminate()
                except Exception:
                    pass
                threading.Thread(target=listen_fm, args=(mhz, name),
                                 daemon=True).start()
                return
            if mpv is None and wav.exists() and wav.stat().st_size > 400_000:
                set_stage(88, "buffering audio")
                mpv = subprocess.Popen(
                    [MPV, str(wav), "--volume=100", "--keep-open=yes",
                     "--force-seekable=yes",
                     f"--title=Radio Tuna â€” {name}"])
                LIVE_PROCS.append(mpv)
                set_stage(100, "")
        try:
            nr.stdin.close()
        except Exception:
            pass
        close_sdr(sdr, st)

    threading.Thread(target=worker, daemon=True).start()


def listen_fm(mhz, name, ifgr=59, rfgain="3"):
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
                      "listening": True, "sync": False,
                      "title": name, "artist": "analog FM â€” stereo v2",
                      "mer_lo": None, "mer_hi": None, "ber": None,
                      "decoder": "fm_stereo v2 (blend)"})
    set_stage(15, "opening the radio (analog FM)")
    STATE["ifgr"], STATE["rfgain"] = ifgr, str(rfgain)

    def worker():
        sdr = st = None
        ant = pick_antenna(mhz, "fm")
        STATE["antenna"] = ANT_NICK.get(ant, ant)
        for attempt in range(4):
            try:
                sdr, st = open_sdr(mhz, ifgr=ifgr, rfgain=str(rfgain),
                                   ant=ant)
                break
            except Exception:
                if GEN[0] != my_gen:
                    return
                set_stage(15, f"radio busy â€” retrying ({attempt + 2}/4)")
                time.sleep(2.5)
        if sdr is None:
            set_stage(0, "RADIO UNAVAILABLE â€” another process holds the "
                         "SDR; stop it and click again")
            STATE.update({"listening": False})
            return
        set_stage(55, "demodulating FM (stereo v2)")
        wav = LAB / "radio_live.wav"
        try:
            wav.unlink()
        except OSError:
            pass
        fh = open(wav, "wb")
        fh.write(fm_stereo.wav_header(fm_stereo.FS_AUDIO, 2))
        fh.flush()
        dem = fm_stereo.FMStereo()
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
        while GEN[0] == my_gen:
            try:
                chunk = iq_q.get(timeout=1.0)
            except queue.Empty:
                continue
            pcm, tele = dem.feed(decimate2(chunk))
            if len(pcm):
                fh.write(pcm.tobytes())
                fh.flush()
            for k in FM_KEYS:
                if k in tele:
                    STATE[k] = tele[k]
            if mpv is None and time.time() - t0 > 2.5:
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
<title>ALBACORE TUNA RADIO</title><style>
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
</style></head><body><div id="cabinet">
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
</div>
<div id="freq">&mdash; &middot; &mdash;</div>
<div id="nowplaying"><span class="t">welcome</span><br>
<span class="a">survey the band, then click a program</span></div>
<div id="dial"><canvas id="dialc" width="1880" height="120"></canvas></div>
<div class="meters">
 <div class="meter"><div class="k">MER LO</div><div class="v" id="mlo">&mdash;</div></div>
 <div class="meter"><div class="k">MER HI</div><div class="v" id="mhi">&mdash;</div></div>
 <div class="meter"><div class="k">BER</div><div class="v" id="ber">&mdash;</div></div>
 <div class="meter"><div class="k">LOCK</div><div class="v" id="lock">&mdash;</div></div>
 <div class="meter"><div class="k">AUDIO</div><div class="v" id="audio">&mdash;</div></div>
</div>
<div style="text-align:center">
 <button class="knob" onclick="survey()">&#x1F4E1; SURVEY THE BAND</button>
 <button class="knob" onclick="stopL()">&#x23F9; STOP</button>
</div>
<div id="status"></div>
<div id="pbar"><div style="width:0%"></div></div>
<details id="nerd" open><summary>STATS FOR NERDS</summary>
<div id="nerdgrid"></div>
<div id="daylab"></div></details>
<div id="guide">loading the guide&hellip;</div>
</div><script>
let stations=[];
async function survey(){document.getElementById('status').textContent=
'surveying: sweeps the band, probes each strong station for HD (~4 min)';
await fetch('/api/survey',{method:'POST'})}
async function stopL(){await fetch('/api/stop',{method:'POST'})}
async function listenFM(mhz,name){
document.getElementById('status').textContent='tuning '+mhz.toFixed(1)+
' analog (stereo v2) - audio in ~4 s';
await fetch('/api/listen_fm',{method:'POST',body:JSON.stringify({mhz,name})})}
async function listen(mhz,prog,name){
document.getElementById('status').textContent='tuning '+mhz.toFixed(1)+
' program '+prog+' - audio in ~8-12 s';
await fetch('/api/listen',{method:'POST',body:JSON.stringify({mhz,prog,name})})}
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
if(s.listening){document.getElementById('nowplaying').innerHTML=
'<span class="t">'+(s.title||s.name||'')+'</span><br><span class="a">'+
(s.artist||'')+'</span>'}
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
if(s.survey&&s.survey.running)document.getElementById('status').textContent=
'[SURVEY] '+s.survey.line+' ('+s.survey.pct+'%)';
const pb=document.getElementById('pbar');
if(s.stage&&s.pct<100){document.getElementById('status').textContent=
(s.pct===0?'[!] ':'[~] ')+s.stage;
pb.style.display='block';pb.firstElementChild.style.width=(s.pct||2)+'%';}
else if(!s.survey||!s.survey.running){pb.style.display='none';
if(s.listening&&s.pct===100)document.getElementById('status').textContent='';}
drawDial(s.mhz);
let ng=ncard('DECODER',s.decoder||'idle');
if(s.antenna)ng+=ncard('ANTENNA (auto)',s.antenna+' ['+
(s.hour_band||'?')+' table]');
if(s.ifgr!=null)ng+=ncard('GAIN IN USE','IFGR '+s.ifgr+' / RF '+s.rfgain);
ng+=ncard('RADIO LOCK',s.lock?(s.lock.owner+': '+
(s.lock.purpose||'')):'free');
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
document.getElementById('nerdgrid').innerHTML=ng;
document.getElementById('daylab').textContent=
s.daylab?('DAY LAB \\u25b8 '+s.daylab):'DAY LAB \\u25b8 idle';
let h='<table>';
for(const st of stations){
h+='<tr><td class="st">'+st.mhz.toFixed(1)+
' '+(st.name||'')+(st.hd?'<span class="hd">HD</span>':'')+
(st.slogan?' <span class="rssi">'+st.slogan+'</span>':'')+'</td><td>';
if(st.hd){const progs=Object.keys(st.programs||{}).length?
Object.entries(st.programs):[["0","HD1"]];
for(const [p,label] of progs){h+='<button class="prog" onclick="listen('+
st.mhz+','+p+',\\''+(st.name||st.mhz)+'\\')">HD'+(parseInt(p)+1)+
' <span style="color:#3f6a78;font-size:10px">'+label+'</span></button>'}}
h+='<button class="prog" style="border-color:#39ff8a" onclick="listenFM('+
st.mhz+',\\''+(st.name||st.mhz)+'\\')">FM</button>';
h+='</td><td class="rssi">+'+st.rssi+' dB'+
(st.mer_lo!=null?' | MER '+st.mer_lo:'')+'</td></tr>'}
document.getElementById('guide').innerHTML=h+'</table>';
}catch(e){}}
setInterval(refresh,1500);refresh();
</script></body></html>"""


_DAYLAB = {"t": 0.0, "line": ""}


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
        elif self.path == "/api/state":
            st = dict(STATE)
            st["survey"] = dict(SURVEY)
            st["daylab"] = daylab_line()
            h = time.gmtime().tm_hour
            st["hour_band"] = "day" if 11 <= h < 19 else "evening"
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
                                   req.get("rfgain", "3")),
                             daemon=True).start()
            self._send('"listening analog"')
        elif self.path == "/api/listen":
            threading.Thread(target=listen,
                             args=(req["mhz"], req["prog"],
                                   req.get("name", ""),
                                   req.get("ifgr", 59),
                                   req.get("rfgain", "3")),
                             daemon=True).start()
            self._send('"listening"')
        elif self.path == "/api/stop":
            threading.Thread(target=stop_listen, daemon=True).start()
            self._send('"stopped"')
        else:
            self.send_error(404)


if __name__ == "__main__":
    print(f"Radio Tuna panel: http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
