"""ais.py - Radio Tuna campaign: AIS ship tracking on 162 MHz.

Every commercial vessel broadcasts AIS position reports: GMSK 9600 bd on
two channels (161.975 / 162.025 MHz), HDLC-framed with a CRC-16 - another
honest truth dial. One 250 kHz capture centered at 162.000 covers BOTH
channels; we mix each to DC and demodulate. Weak-at-range over water is
the adaptive-decode target (the Potomac and the Bay are down the road).

Pipeline per channel: mix -> resample 48k (5 sps) -> FM discriminator ->
bit sync -> NRZI -> HDLC flag hunt + destuff -> CRC-16/X.25 gate ->
AIS payload (type, MMSI, position, speed).

Modes:
  selftest - full synthetic roundtrip: encode a position report through
             CRC+stuff+NRZI+GMSK, add noise+offset, decode it back
  capture  - N seconds live, decode both channels, ship table

Example:  python ais.py capture --secs 60 --antenna "Antenna C"
"""
import argparse
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

try:
    from numba import njit
    _HAVE_NUMBA = True
except Exception:
    _HAVE_NUMBA = False

HERE = Path(__file__).resolve().parent
LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)

FS = 250_000.0
CENTER = 162.000e6
CHAN_OFF = {"A": -25_000.0, "B": +25_000.0}   # 161.975 / 162.025
BAUD = 9600.0


def _ensure_sdr_dll_path():
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


_ensure_sdr_dll_path()


# ==========================================================================
# CRC-16/X.25 (reflected 0x1021, init/xorout 0xFFFF) over bytes
# ==========================================================================
def crc16_x25(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


# ==========================================================================
# bit plumbing
# ==========================================================================
def _rev8(b):
    r = 0
    for i in range(8):
        r = (r << 1) | ((b >> i) & 1)
    return r


def stuff(bits):
    out = []
    run = 0
    for b in bits:
        out.append(b)
        run = run + 1 if b == 1 else 0
        if run == 5:
            out.append(0)
            run = 0
    return out


def destuff(bits):
    out = []
    run = 0
    i = 0
    while i < len(bits):
        b = bits[i]
        out.append(b)
        run = run + 1 if b == 1 else 0
        if run == 5:
            i += 1              # skip the stuffed 0
            if i < len(bits) and bits[i] == 1:
                return None     # 6 ones = flag/abort inside frame
            run = 0
        i += 1
    return out


def nrzi_encode(bits):
    """AIS NRZI: 0 = transition, 1 = no transition."""
    out = []
    cur = 0
    for b in bits:
        if b == 0:
            cur ^= 1
        out.append(cur)
    return out


def nrzi_decode(line_bits):
    out = np.empty(len(line_bits) - 1, np.int8)
    for i in range(1, len(line_bits)):
        out[i - 1] = 1 if line_bits[i] == line_bits[i - 1] else 0
    return out


# ==========================================================================
# HDLC frame hunt on a decoded NRZI bitstream
# ==========================================================================
FLAG = [0, 1, 1, 1, 1, 1, 1, 0]


def find_frames(bits):
    """Scan for 0x7E...0x7E frames; return payload byte arrays that pass
    CRC-16/X.25. Bytes assemble LSB-first per HDLC convention."""
    s = "".join(str(int(b)) for b in bits)
    flag = "01111110"
    hits = []
    idx = [i for i in range(len(s) - 8) if s[i:i + 8] == flag]
    for a_i in range(len(idx)):
        for b_i in range(a_i + 1, min(a_i + 8, len(idx))):
            a, b = idx[a_i] + 8, idx[b_i]
            n = b - a
            if not (160 <= n <= 450):        # AIS type1-3 ~= 184+16 stuffed
                continue
            inner = [int(c) for c in s[a:b]]
            raw = destuff(inner)
            if raw is None or len(raw) % 8 != 0 or len(raw) < 48:
                continue
            by = bytes(_rev8(int("".join(str(x) for x in raw[k:k + 8]), 2))
                       for k in range(0, len(raw), 8))
            if len(by) < 5:
                continue
            body, fcs = by[:-2], by[-2] | (by[-1] << 8)
            if crc16_x25(body) == fcs:
                hits.append(body)
    return hits


def parse_ais(body):
    """AIS payload bits = frame bytes read MSB-first (the HDLC LSB-first
    wire order was already undone when the bytes were assembled)."""
    bits = []
    for byte in body:
        for i in range(8):
            bits.append((byte >> (7 - i)) & 1)
    def bf(a, b, signed=False):
        v = 0
        for i in range(a, b):
            v = (v << 1) | bits[i]
        if signed and bits[a]:
            v -= 1 << (b - a)
        return v
    t = bf(0, 6)
    out = {"type": t, "mmsi": bf(8, 38)}
    if t in (1, 2, 3) and len(bits) >= 168:
        out["sog_kt"] = bf(50, 60) / 10.0
        out["lon"] = bf(61, 89, signed=True) / 600000.0
        out["lat"] = bf(89, 116, signed=True) / 600000.0
    elif t == 4 and len(bits) >= 168:      # base station report
        out["lon"] = bf(79, 107, signed=True) / 600000.0
        out["lat"] = bf(107, 134, signed=True) / 600000.0
    elif t == 21 and len(bits) >= 272:     # aid-to-navigation
        cs = ""
        for k in range(20):
            c = bf(43 + 6 * k, 49 + 6 * k)
            cs += "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"[c]
        out["name"] = cs.replace("@", "").strip()
        out["lon"] = bf(164, 192, signed=True) / 600000.0
        out["lat"] = bf(192, 219, signed=True) / 600000.0
    return out


# ==========================================================================
# GMSK demod (shared style with the sonde tool)
# ==========================================================================
def _bitsync_impl(disc, sps):
    N = disc.shape[0]
    nb = int(N / sps) - 2
    soft = np.empty(nb, np.float32)
    pos = 0.0
    isps = int(sps)
    for k in range(nb):
        p = int(pos)
        if p + isps >= N:
            nb = k
            break
        acc = 0.0
        for j in range(isps):
            acc += disc[p + j]
        soft[k] = acc
        h1 = 0.0
        h2 = 0.0
        half = isps // 2
        for j in range(half):
            h1 += disc[p + j]
            h2 += disc[p + half + j]
        if soft[k] > 0:
            pos += sps + (0.05 if h2 > h1 else -0.05)
        else:
            pos += sps + (0.05 if h1 > h2 else -0.05)
    return soft[:nb]


_bitsync = njit(cache=True)(_bitsync_impl) if _HAVE_NUMBA else _bitsync_impl


def demod_channel(iq, fs, chan_off_hz):
    from scipy.signal import resample_poly
    from math import gcd
    n = np.arange(len(iq), dtype=np.float64)
    x = (iq * np.exp(-2j * np.pi * chan_off_hz / fs * n)).astype(np.complex64)
    target = 48_000
    g = gcd(int(target), int(fs))
    x = resample_poly(x, int(target) // g, int(fs) // g).astype(np.complex64)
    disc = np.angle(x[1:] * np.conj(x[:-1])).astype(np.float32)
    disc -= np.float32(np.mean(disc))
    soft = _bitsync(disc, target / BAUD)
    ships = []
    for sgn in (1.0, -1.0):                 # polarity fallback
        line = (soft * sgn > 0).astype(np.int8)
        bits = nrzi_decode(line)
        for body in find_frames(bits):
            d = parse_ais(body)
            if 0 < d["mmsi"] < 999_999_999:
                ships.append(d)
        if ships:
            break
    return ships


# ==========================================================================
# selftest: full encode->decode roundtrip
# ==========================================================================
def encode_frame_iq(mmsi, lat, lon, fs=250_000.0, chan="A", noise=0.05):
    """Mirror of the receive chain, for proof."""
    bits = [0] * 168
    def sb(a, b, v):
        if v < 0:
            v += 1 << (b - a)
        for i in range(b - 1, a - 1, -1):
            bits[i] = v & 1
            v >>= 1
    sb(0, 6, 1)
    sb(8, 38, mmsi)
    sb(50, 60, 123)                        # 12.3 kt
    sb(61, 89, int(lon * 600000))
    sb(89, 116, int(lat * 600000))
    by = bytes(int("".join(str(x) for x in bits[k:k + 8]), 2)
               for k in range(0, 168, 8))
    fcs = crc16_x25(by)
    frame_bytes = by + bytes([fcs & 0xFF, fcs >> 8])
    fb = []
    for byte in frame_bytes:
        for i in range(8):
            fb.append((byte >> i) & 1)     # LSB-first on the wire
    wire = [0, 1] * 12 + FLAG + stuff(fb) + FLAG + [0, 1] * 4
    line = nrzi_encode(wire)
    sps = fs / BAUD
    dev = 2400.0
    total = int(len(line) * sps) + 400
    freq = np.zeros(total, np.float32)
    for i, b in enumerate(line):
        a, z = int(i * sps), int((i + 1) * sps)
        freq[a:z] = dev if b else -dev
    phase = np.cumsum(2 * np.pi * freq / fs)
    iq = 0.5 * np.exp(1j * phase).astype(np.complex64)
    off = CHAN_OFF[chan]
    n = np.arange(len(iq))
    iq = iq * np.exp(2j * np.pi * (off + 300) / fs * n)   # +300 Hz error
    rng = np.random.default_rng(2)
    iq = iq + (rng.normal(0, noise, len(iq)) + 1j * rng.normal(0, noise, len(iq))
               ).astype(np.complex64)
    return iq.astype(np.complex64)


def cmd_selftest(args):
    print("=" * 62)
    print("Radio Tuna AIS self-test (encode -> GMSK -> decode roundtrip)")
    print("=" * 62)
    ok = True
    print("[1] CRC-16/X.25 sanity")
    c = crc16_x25(b"123456789")
    print(f"    check value: {c:04X}  {'OK' if c == 0x906E else 'FAIL'}")
    ok &= (c == 0x906E)
    print("[2] synthetic position report, both channels, noise + 300 Hz offset")
    for chan in ("A", "B"):
        iq = encode_frame_iq(367_123_450, 38.85, -77.02, chan=chan)
        ships = demod_channel(iq, FS, CHAN_OFF[chan])
        hit = any(s["mmsi"] == 367_123_450 and abs(s.get("lat", 0) - 38.85) < 0.001
                  for s in ships)
        print(f"    channel {chan}: decoded={len(ships)} "
              f"mmsi/pos match={'OK' if hit else 'FAIL'}")
        ok &= hit
    print("=" * 62)
    print("SELFTEST", "PASS" if ok else "FAIL")
    print("=" * 62)
    return 0 if ok else 1


# ==========================================================================
# live capture
# ==========================================================================
def cmd_capture(args):
    import SoapySDR
    from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CS16
    SoapySDR.SoapySDR_setLogLevel(SoapySDR.SOAPY_SDR_FATAL)
    sdr = SoapySDR.Device("driver=sdrplay")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setFrequency(SOAPY_SDR_RX, 0, CENTER)
    try:
        sdr.setAntenna(SOAPY_SDR_RX, 0, args.antenna)
    except Exception:
        pass
    try:
        sdr.setGainMode(SOAPY_SDR_RX, 0, False)
        sdr.setGain(SOAPY_SDR_RX, 0, "IFGR", 22)
        sdr.writeSetting("rfgain_sel", "0")
    except Exception:
        pass
    st = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16)
    sdr.activateStream(st)
    print(f"[capture] {args.secs:.0f}s @ 162.000 MHz (both AIS channels) "
          f"on {args.antenna}")
    n_want = int(args.secs * FS)
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
    sdr.deactivateStream(st)
    sdr.closeStream(st)
    iq = ((out[0::2].astype(np.float32) + 1j * out[1::2].astype(np.float32))
          / 32768.0).astype(np.complex64)[:got]
    print(f"[capture] {len(iq)/FS:.1f}s captured, demodulating A+B ...")
    ships = {}
    counts = {}
    for chan in ("A", "B"):
        found = demod_channel(iq, FS, CHAN_OFF[chan])
        counts[chan] = len(found)
        for s in found:
            e = ships.setdefault(s["mmsi"], {"msgs": 0})
            e["msgs"] += 1
            e.update({k: v for k, v in s.items() if k != "mmsi"})
    print(f"[result] CRC-valid frames: A={counts['A']} B={counts['B']}")
    if ships:
        print(f"[ships] {len(ships)} vessel(s):")
        for mmsi, s in ships.items():
            pos = (f"{s['lat']:.4f},{s['lon']:.4f}"
                   if "lat" in s else "?")
            nm = f"  name={s['name']}" if s.get("name") else ""
            print(f"    MMSI {mmsi}  msgs={s['msgs']}  type={s.get('type')}"
                  f"  pos={pos}  sog={s.get('sog_kt', '?')} kt{nm}")
    else:
        print("[ships] none decoded (rivers are quiet from indoors; try a")
        print("        longer capture, or this stays armed for the harbor trip)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    c = sub.add_parser("capture")
    c.add_argument("--secs", type=float, default=60)
    c.add_argument("--antenna", default="Antenna C")
    args = ap.parse_args()
    if args.cmd == "selftest":
        sys.exit(cmd_selftest(args))
    elif args.cmd == "capture":
        cmd_capture(args)


if __name__ == "__main__":
    main()
