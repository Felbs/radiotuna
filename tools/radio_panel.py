"""radio_panel.py â€” Radio Tuna's listening room.  http://localhost:8643

A classic receiver, rendered: big frequency readout, a tuning dial with
every station the band survey found, HD subchannel buttons (the "grid"),
live now-playing metadata scraped from the digital stream, and MER/BER
meters. TV Tuna panel skeleton in a vintage cabinet.

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

HERE = Path(__file__).resolve().parent
LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
PY = sys.executable          # run helpers with the same python (radioconda)
import os as _os
import shutil as _sh
NRSC5 = (_os.environ.get("NRSC5_EXE") or _sh.which("nrsc5")
         or r"C:\Tools\nrsc5\nrsc5.exe")
MPV = (_os.environ.get("MPV_EXE") or _sh.which("mpv")
       or r"C:\Program Files\MPV Player\mpv.exe")
STATIONS = LAB / "stations.json"
PORT = 8643
FS_NRSC5 = 1_488_375.0
FS_CAP = 2 * FS_NRSC5

STATE = {"mhz": None, "prog": None, "name": None, "listening": False,
         "title": None, "artist": None, "mer_lo": None, "mer_hi": None,
         "ber": None, "sync": False, "stage": "", "pct": 0,
         "audio": None}


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


def open_sdr(mhz, ifgr=59.0, rfgain="3", rate=FS_CAP):
    _ensure_sdr_dll_path()
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, rate)
    sdr.setFrequency(SOAPY_SDR_RX, 0, mhz * 1e6)
    sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
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
            sdr.setAntenna(SOAPY_SDR_RX, 0, "Antenna A")
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
                      "name": None, "sync": False, "stage": "", "pct": 0})
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
                         errors="replace")
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
        for attempt in range(4):          # post-restart contention retry
            try:
                sdr, st = open_sdr(mhz, ifgr=ifgr, rfgain=str(rfgain))
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
            stderr=subprocess.STDOUT, text=False)
        LIVE_PROCS.append(nr)
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
    """Analog FM v1: pure-numpy WFM demod (mono + 75us de-emphasis) at
    the pump, growing WAV, mpv tails it. Stereo + pilot-SNR telemetry
    arrive with the gr-based path (campaign upgrade)."""
    stop_listen()
    time.sleep(1)
    with LOCK:
        GEN[0] += 1
        my_gen = GEN[0]
        STATE.update({"mhz": mhz, "prog": None, "name": name + " (analog)",
                      "listening": True, "sync": False,
                      "title": name, "artist": "analog FM â€” mono v1",
                      "mer_lo": None, "mer_hi": None, "ber": None})
    set_stage(15, "opening the radio (analog FM)")

    def worker():
        import struct
        sdr = st = None
        for attempt in range(4):
            try:
                sdr, st = open_sdr(mhz, ifgr=ifgr, rfgain=str(rfgain))
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
        set_stage(55, "demodulating FM")
        wav = LAB / "radio_live.wav"
        try:
            wav.unlink()
        except OSError:
            pass
        AUDIO_FS = int(FS_CAP / 2 / 16)     # 1.488M/16 = 93,023 Hz
        fh = open(wav, "wb")
        # WAV header with a huge declared size (grows like nrsc5's)
        fh.write(b"RIFF" + struct.pack("<I", 0x7FFFFFF0) + b"WAVE"
                 + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1,
                                         AUDIO_FS, AUDIO_FS * 2, 2, 16)
                 + b"data" + struct.pack("<I", 0x7FFFFF00))
        fh.flush()
        buf = np.empty(2 * 65536, np.int16)
        prev = np.complex64(1 + 0j)
        de = 0.0
        agc = 60000.0                             # adaptive output gain
        alpha = 1.0 / (1.0 + 75e-6 * AUDIO_FS)   # 75us de-emphasis pole
        mpv = None
        t0 = time.time()
        while GEN[0] == my_gen:
            r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
            if r.ret <= 0:
                continue
            n = r.ret
            raw = decimate2(buf[:2 * n])          # -> 1.488 MS/s cs16
            x = (raw[0::2].astype(np.float32)
                 + 1j * raw[1::2].astype(np.float32))
            if len(x) < 64:
                continue
            xd = np.empty(len(x) + 1, np.complex64)
            xd[0] = prev
            xd[1:] = x
            prev = x[-1]
            d = np.angle(xd[1:] * np.conj(xd[:-1]))   # FM discriminator
            k = (len(d) // 16) * 16
            a = d[:k].reshape(-1, 16).mean(axis=1)    # -> ~93 kHz audio
            # one-pole de-emphasis
            out = np.empty(len(a), np.float32)
            acc = de
            for i in range(len(a)):
                acc += alpha * (a[i] - acc)
                out[i] = acc
            de = float(acc)
            # slow AGC: ride any station to ~17% FS rms, never clip peaks
            r_ = float(np.sqrt((out ** 2).mean())) + 1e-9
            want = min(max(5500.0 / r_, 12000.0), 400000.0)
            agc += 0.06 * (want - agc)
            pcm = np.clip(out * agc, -32000, 32000).astype(np.int16)
            fh.write(pcm.tobytes())
            fh.flush()
            if mpv is None and time.time() - t0 > 2.5:
                mpv = subprocess.Popen(
                    [MPV, str(wav), "--volume=100", "--keep-open=yes",
                     "--force-seekable=yes",
                     f"--title=Radio Tuna â€” {name} (FM)"])
                LIVE_PROCS.append(mpv)
                set_stage(100, "")
        fh.close()
        close_sdr(sdr, st)

    threading.Thread(target=worker, daemon=True).start()


# â”€â”€ the page â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Radio Tuna ðŸŸðŸ“»</title><style>
body{font-family:Georgia,'Times New Roman',serif;background:
radial-gradient(ellipse at top,#3b2a1a,#1d130a 70%);color:#e8d5a3;
margin:0;padding:18px;min-height:100vh}
h1{font-size:26px;margin:0;letter-spacing:2px;color:#f4e3b2;
text-shadow:0 0 12px rgba(244,196,110,.35)}
.sub{color:#a8895c;font-size:12px;margin-bottom:14px;font-style:italic}
#cabinet{max-width:980px;margin:0 auto;background:linear-gradient(
#2b1d10,#241708);border:3px solid #58402a;border-radius:18px;
box-shadow:0 10px 40px rgba(0,0,0,.6),inset 0 1px 0 #6b4f2f;padding:20px}
#freq{font-family:'Courier New',monospace;font-size:52px;color:#ffb84d;
text-align:center;text-shadow:0 0 18px rgba(255,170,60,.55);margin:6px 0}
#nowplaying{text-align:center;min-height:44px;color:#f4e3b2}
#nowplaying .t{font-size:19px}
#nowplaying .a{font-size:13px;color:#c8a86e;font-style:italic}
#dial{position:relative;height:64px;background:linear-gradient(#191008,
#100a05);border:2px solid #58402a;border-radius:10px;margin:14px 0}
#dial canvas{width:100%;height:100%;display:block}
.meters{display:flex;gap:14px;justify-content:center;margin:10px 0}
.meter{background:#191008;border:2px solid #58402a;border-radius:10px;
padding:6px 16px;text-align:center;min-width:90px}
.meter .k{font-size:10px;color:#a8895c;letter-spacing:1px}
.meter .v{font-family:'Courier New',monospace;font-size:20px;color:#ffb84d}
button{cursor:pointer;font-family:Georgia,serif}
.knob{background:linear-gradient(#6b4f2f,#4a3319);color:#f4e3b2;border:
1px solid #8a6a40;border-radius:8px;padding:7px 18px;font-size:14px}
.knob:hover{background:linear-gradient(#7d5d38,#58402a)}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:12px}
td{padding:7px 8px;border-bottom:1px solid #3a2917;vertical-align:middle}
.st{font-size:16px;color:#f4e3b2}
.hd{display:inline-block;background:#8a6a40;color:#1d130a;font-weight:
bold;font-size:10px;border-radius:4px;padding:1px 6px;margin-left:6px}
.prog{background:#191008;color:#e8d5a3;border:1px solid #6b4f2f;
border-radius:6px;padding:4px 12px;margin:2px;font-size:13px}
.prog:hover{background:#3a2917;color:#ffb84d}
.rssi{color:#a8895c;font-family:'Courier New',monospace;font-size:12px}
#status{text-align:center;color:#c8a86e;font-size:13px;min-height:20px;
margin-top:6px}
#pbar{height:8px;background:#191008;border:1px solid #58402a;
border-radius:5px;margin:6px 15%;overflow:hidden;display:none}
#pbar div{height:100%;background:linear-gradient(90deg,#8a6a40,#ffb84d);
transition:width .8s;border-radius:5px}
</style></head><body><div id="cabinet">
<h1>RADIO TUNA <span style="font-size:15px">ðŸŸðŸ“» high definition receiver</span></h1>
<div class="sub">adaptive decoding Â· vacuum tubes not included</div>
<div style="margin:4px 0 10px">
 <button class="knob" style="background:linear-gradient(#8a6a40,#58402a)">FM Â· HD</button>
 <button class="knob" style="opacity:.45" title="campaign pending">AM â€” soon</button>
 <button class="knob" style="opacity:.45" title="campaign pending">SW â€” soon</button>
</div>
<div id="freq">â€” Â· â€”</div>
<div id="nowplaying"><span class="t">welcome</span><br>
<span class="a">survey the band, then click a program</span></div>
<div id="dial"><canvas id="dialc" width="1880" height="120"></canvas></div>
<div class="meters">
 <div class="meter"><div class="k">MER LO</div><div class="v" id="mlo">â€”</div></div>
 <div class="meter"><div class="k">MER HI</div><div class="v" id="mhi">â€”</div></div>
 <div class="meter"><div class="k">BER</div><div class="v" id="ber">â€”</div></div>
 <div class="meter"><div class="k">LOCK</div><div class="v" id="lock">â€”</div></div>
 <div class="meter"><div class="k">AUDIO</div><div class="v" id="audio">â€”</div></div>
</div>
<div style="text-align:center">
 <button class="knob" onclick="survey()">ðŸ“¡ SURVEY THE BAND</button>
 <button class="knob" onclick="stopL()">â¹ STOP</button>
</div>
<div id="status"></div>
<div id="pbar"><div style="width:0%"></div></div>
<div id="guide">loading the guideâ€¦</div>
</div><script>
let stations=[];
async function survey(){document.getElementById('status').textContent=
'surveying â€” sweeps the band, probes each strong station for HD (~4 min)â€¦';
await fetch('/api/survey',{method:'POST'})}
async function stopL(){await fetch('/api/stop',{method:'POST'})}
async function listenFM(mhz,name){
document.getElementById('status').textContent='tuning '+mhz.toFixed(1)+
' analog â€” audio in ~4 sâ€¦';
await fetch('/api/listen_fm',{method:'POST',body:JSON.stringify({mhz,name})})}
async function listen(mhz,prog,name){
document.getElementById('status').textContent='tuning '+mhz.toFixed(1)+
' program '+prog+' â€” audio in ~8-12 sâ€¦';
await fetch('/api/listen',{method:'POST',body:JSON.stringify({mhz,prog,name})})}
function drawDial(cur){
const c=document.getElementById('dialc'),g=c.getContext('2d');
g.fillStyle='#0e0903';g.fillRect(0,0,c.width,c.height);
const x=m=>((m-87.5)/(108.3-87.5))*c.width;
g.strokeStyle='#58402a';g.fillStyle='#a8895c';
g.font='16px Courier New';
for(let m=88;m<=108;m+=2){g.beginPath();
g.moveTo(x(m),0);g.lineTo(x(m),22);g.stroke();
g.fillText(m,x(m)-12,44)}
for(const s of stations){const px=x(s.mhz);
g.fillStyle=s.hd?'#ffb84d':'#6b4f2f';
g.beginPath();g.arc(px,78,s.hd?7:4,0,7);g.fill()}
if(cur){g.strokeStyle='#ff4d1c';g.lineWidth=3;g.beginPath();
g.moveTo(x(cur),0);g.lineTo(x(cur),c.height);g.stroke();g.lineWidth=1}}
async function refresh(){try{
const s=await (await fetch('/api/state')).json();
stations=s.stations||[];
document.getElementById('freq').textContent=
s.mhz?s.mhz.toFixed(1)+' FM':'â€” Â· â€”';
if(s.listening){document.getElementById('nowplaying').innerHTML=
'<span class="t">'+(s.title||s.name||'')+'</span><br><span class="a">'+
(s.artist||'')+'</span>'}
document.getElementById('mlo').textContent=s.mer_lo??'â€”';
document.getElementById('mhi').textContent=s.mer_hi??'â€”';
document.getElementById('ber').textContent=s.ber!=null?s.ber.toFixed(4):'â€”';
document.getElementById('lock').textContent=s.sync?'â—':'â€”';
document.getElementById('lock').style.color=s.sync?'#7dff8a':'#a8895c';
const au=document.getElementById('audio');
au.textContent=s.audio==='MUSIC/SPEECH'?'â™ª':(s.audio==='STATIC'?'âœ—':
(s.audio==='SILENCE'?'â€¦':(s.audio||'â€”')));
au.style.color=s.audio==='MUSIC/SPEECH'?'#7dff8a':
(s.audio==='STATIC'?'#ff6b4d':'#a8895c');
if(s.survey&&s.survey.running)document.getElementById('status').textContent=
'ðŸ“¡ '+s.survey.line+' ('+s.survey.pct+'%)';
const pb=document.getElementById('pbar');
if(s.stage&&s.pct<100){document.getElementById('status').textContent=
(s.pct===0?'ðŸ”´ ':'ðŸŽ› ')+s.stage;
pb.style.display='block';pb.firstElementChild.style.width=(s.pct||2)+'%';}
else if(!s.survey||!s.survey.running){pb.style.display='none';
if(s.listening&&s.pct===100)document.getElementById('status').textContent='';}
drawDial(s.mhz);
let h='<table>';
for(const st of stations){
h+='<tr><td class="st">'+st.mhz.toFixed(1)+
' '+(st.name||'')+(st.hd?'<span class="hd">HD</span>':'')+
(st.slogan?' <span class="rssi">'+st.slogan+'</span>':'')+'</td><td>';
if(st.hd){const progs=Object.keys(st.programs||{}).length?
Object.entries(st.programs):[["0","HD1"]];
for(const [p,label] of progs){h+='<button class="prog" onclick="listen('+
st.mhz+','+p+',\\''+(st.name||st.mhz)+'\\')">HD'+(parseInt(p)+1)+
' <span style="color:#a8895c;font-size:10px">'+label+'</span></button>'}}
h+='<button class="prog" style="border-color:#4a6a40" onclick="listenFM('+
st.mhz+',\\''+(st.name||st.mhz)+'\\')">FM</button>';
h+='</td><td class="rssi">+'+st.rssi+' dB'+
(st.mer_lo!=null?' Â· MER '+st.mer_lo:'')+'</td></tr>'}
document.getElementById('guide').innerHTML=h+'</table>';
}catch(e){}}
setInterval(refresh,1500);refresh();
</script></body></html>"""


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
