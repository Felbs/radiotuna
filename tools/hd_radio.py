"""hd_radio.py — Radio Tuna campaign 1: HD Radio via SDRplay + nrsc5.

The bridge: RSPdx captures IQ (SoapySDR), we decimate to nrsc5's native
1,488,375 Hz, nrsc5 decodes NRSC-5 and reports the truth-dial (CNR, BER,
sync) on stderr — which we surface exactly like the TV chain's telemetry.

Modes:
  capture  — N seconds of IQ around an FM station to a .cs16 file
  decode   — run nrsc5 on a capture; audio to WAV + stats to console
  live     — real-time listen: SDR -> decimator -> nrsc5 -> speakers
             (WAV-file tail played by mpv, same trick as tv_watch)

Examples:
  python hd_radio.py capture --mhz 103.5 --secs 15
  python hd_radio.py decode  --iq lab/hd_1035_*.cs16 --mhz 103.5 --prog 0
  python hd_radio.py live    --mhz 103.5 --prog 0
"""
import argparse
import glob
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LAB = HERE.parent / "lab"          # project-root lab/, not tools/lab
LAB.mkdir(exist_ok=True)
import os as _os
import shutil as _sh
NRSC5 = (_os.environ.get("NRSC5_EXE") or _sh.which("nrsc5")
         or r"C:\Tools\nrsc5\nrsc5.exe")
MPV = (_os.environ.get("MPV_EXE") or _sh.which("mpv")
       or r"C:\Program Files\MPV Player\mpv.exe")
FS_NRSC5 = 1_488_375.0
FS_CAP = 2 * FS_NRSC5          # capture at 2x, decimate by 2


def _ensure_sdr_dll_path():
    """Bare (non-activated) python can't find the SoapySDR driver DLLs -
    the standard shim every other tool in the family carries."""
    import os
    if os.name != "nt":
        return
    root = Path(sys.executable).resolve().parent
    for p in (root / "Library" / "bin",
              Path(r"C:\Program Files\SDRplay\API\x64"),
              Path(r"C:\Program Files\SDRplay\API")):
        if p.is_dir():
            os.environ["PATH"] = str(p) + os.pathsep + os.environ["PATH"]
            try:
                os.add_dll_directory(str(p))
            except Exception:
                pass


def open_sdr(mhz, ifgr=40.0, rfgain="3"):
    _ensure_sdr_dll_path()
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS_CAP)
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
    return sdr, st, SOAPY_SDR_RX


def cs16_to_cu8(raw_i16):
    """This nrsc5 build's cs16 file path is silent/broken; its cu8 path
    (the RTL-native dialect everyone uses) works — convert on write."""
    return ((raw_i16.astype(np.int32) >> 8) + 128).clip(0, 255).astype(np.uint8)


def decimate2_cs16(raw):
    """interleaved int16 IQ at 2x -> halfband-ish decimated cs16 at 1x.
    Simple 2-tap average before decimation — adequate anti-alias for a
    signal occupying ~400 kHz of a 1.49 MHz output rate."""
    i = raw[0::2].astype(np.int32)
    q = raw[1::2].astype(np.int32)
    i2 = ((i[0::2] + i[1::2]) // 2).astype(np.int16)
    q2 = ((q[0::2] + q[1::2]) // 2).astype(np.int16)
    out = np.empty(2 * len(i2), np.int16)
    out[0::2] = i2
    out[1::2] = q2
    return out


def cmd_capture(args):
    sdr, st, RX = open_sdr(args.mhz, args.ifgr, args.rfgain)
    import SoapySDR
    n_want = int(args.secs * FS_CAP)
    buf = np.empty(2 * 65536, np.int16)
    stamp = time.strftime("%H%M%S")
    out = LAB / f"hd_{str(args.mhz).replace('.', '')}_{stamp}.cu8"
    got = 0
    with open(out, "wb") as f:
        while got < n_want:
            r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
            if r.ret > 0:
                n = min(r.ret, n_want - got)
                f.write(cs16_to_cu8(decimate2_cs16(buf[:2 * n])).tobytes())
                got += n
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    print(f"captured {got / FS_CAP:.1f}s -> {out}")
    return out


def run_nrsc5(iq_path, mhz, prog, wav_path, live_stats=True):
    cmd = [NRSC5, "-r", str(iq_path), "-o", str(wav_path), str(prog)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         errors="replace")
    stats = {"sync": False, "mer_lo": None, "mer_hi": None,
             "ber": None, "title": None, "station": None}
    for line in p.stdout:
        line = line.rstrip()
        if live_stats:
            print("  " + line, flush=True)
        if "Synchronized" in line:
            stats["sync"] = True
        if "MER:" in line:
            try:
                parts = line.split("MER:")[1]
                lo = float(parts.split("dB")[0])
                stats["mer_lo"] = lo
            except (ValueError, IndexError):
                pass
        if "BER:" in line:
            try:
                stats["ber"] = float(
                    line.split("BER:")[1].split(",")[0])
            except (ValueError, IndexError):
                pass
        if "Title:" in line:
            stats["title"] = line.split("Title:")[1].strip()
        if "Station name:" in line:
            stats["station"] = line.split("Station name:")[1].strip()
    p.wait()
    return stats


def cmd_decode(args):
    iq = Path(sorted(glob.glob(args.iq))[-1])
    wav = iq.with_suffix(".wav")
    print(f"decoding {iq.name} @ {args.mhz} MHz program {args.prog} ...")
    stats = run_nrsc5(iq, args.mhz, args.prog, wav)
    print("\n=== HD VERDICT ===")
    print(f"  sync: {stats['sync']}  MER: {stats['mer_lo']}  "
          f"BER: {stats['ber']}")
    print(f"  station: {stats['station']}  title: {stats['title']}")
    if wav.exists() and wav.stat().st_size > 100_000:
        print(f"  audio: {wav} ({wav.stat().st_size // 1024} KB)")
        if args.play:
            subprocess.Popen([MPV, str(wav), "--volume=110"])
    else:
        print("  audio: none decoded")


def cmd_live(args):
    """Real-time: SDR -> decimate -> named pipe? nrsc5 -r reads a file.
    tv_watch trick instead: capture rolls to a growing .cs16 while nrsc5
    reads it as a file that keeps growing; audio WAV also grows and mpv
    tails it. Latency ~2-4 s. Ctrl+C stops."""
    sdr, st, RX = open_sdr(args.mhz)
    iq_path = LAB / "hd_live.cu8"
    wav = LAB / "hd_live.wav"
    for f in (iq_path, wav):
        try:
            f.unlink()
        except OSError:
            pass
    stop = threading.Event()

    def pump():
        buf = np.empty(2 * 65536, np.int16)
        with open(iq_path, "wb") as f:
            while not stop.is_set():
                r = sdr.readStream(st, [buf], 65536, timeoutUs=500000)
                if r.ret > 0:
                    f.write(cs16_to_cu8(decimate2_cs16(buf[:2 * r.ret])).tobytes())
                    f.flush()

    threading.Thread(target=pump, daemon=True).start()
    time.sleep(3)
    print("starting nrsc5 (stats below) — mpv follows the audio…",
          flush=True)
    nr = subprocess.Popen(
        [NRSC5, "-r", str(iq_path), "-o", str(wav), str(args.prog)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        errors="replace")
    mpv_started = False
    try:
        for line in nr.stdout:
            print("  " + line.rstrip(), flush=True)
            if not mpv_started and wav.exists() \
                    and wav.stat().st_size > 200_000:
                subprocess.Popen([MPV, str(wav), "--volume=110",
                                  "--keep-open=yes",
                                  "--force-seekable=yes"])
                mpv_started = True
    except KeyboardInterrupt:
        pass
    stop.set()
    nr.terminate()
    sdr.deactivateStream(st)
    sdr.closeStream(st)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--mhz", type=float, required=True)
    c.add_argument("--secs", type=float, default=15)
    c.add_argument("--ifgr", type=float, default=40.0)
    c.add_argument("--rfgain", default="3")
    d = sub.add_parser("decode")
    d.add_argument("--iq", required=True)
    d.add_argument("--mhz", type=float, required=True)
    d.add_argument("--prog", type=int, default=0)
    d.add_argument("--play", action="store_true")
    lv = sub.add_parser("live")
    lv.add_argument("--mhz", type=float, required=True)
    lv.add_argument("--prog", type=int, default=0)
    args = ap.parse_args()
    if args.cmd == "capture":
        cmd_capture(args)
    elif args.cmd == "decode":
        cmd_decode(args)
    elif args.cmd == "live":
        cmd_live(args)


if __name__ == "__main__":
    main()
