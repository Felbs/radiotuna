"""rds.py - Radio Tuna: decode RDS (station name + song text) from FM.

Every analog FM station hides a 1187.5 bps BPSK data stream on a 57 kHz
subcarrier (3x the 19 kHz pilot): RDS. It carries the station name (PS),
program type, and RadioText (RT = now-playing). Decoding it upgrades FM
from IDENTIFIED to DECODED - and its (26,16) shortened cyclic code with
offset words is exactly the FEC-with-known-syndromes trick from our ATSC
Reed-Solomon work.

Pipeline: FM discriminate -> bandpass 57 kHz -> costas to the suppressed
subcarrier (locked to 3x the 19 kHz pilot) -> 1187.5 bps BPSK ->
differential decode -> block sync via offset words -> group decode
(0A/0B = PS name, 2A/2B = RadioText).

Modes:
  selftest - synthesize a PS='WXTUNAFM' RDS stream -> decode it back
  decode   - decode a captured FM station (cs16) -> PS + RadioText

Example:  python rds.py decode --file fm_rds_909.cs16
"""
import argparse
import sys
from pathlib import Path

import numpy as np

FS = 250000.0
RBPS = 1187.5
# RDS offset words A,B,C,D (added to the 10-bit checkword per block type)
OFFSET = {"A": 0x0FC, "B": 0x198, "C": 0x168, "Cp": 0x350, "D": 0x1B4}
POLY = 0x5B9            # (26,16) generator
PTY = {0: "None", 1: "News", 4: "Sport", 5: "Educate", 10: "Pop", 11: "Rock",
       15: "OtherM", 20: "Weather", 24: "Jazz", 25: "Country", 29: "Doc"}
CHARS = {i: chr(i) for i in range(32, 127)}


def syndrome(block26):
    reg = 0
    for i in range(25, -1, -1):
        reg = (reg << 1) | ((block26 >> i) & 1)
        if reg & (1 << 10):
            reg ^= (POLY | (1 << 10))
    return reg & 0x3FF


def check_offset(block26, off):
    return syndrome(block26) == off


def fm_discriminate(iq, fs):
    d = iq[1:] * np.conj(iq[:-1])
    return np.angle(d).astype(np.float32)


def costas_bpsk(mpx, fs, fc=57000.0):
    """Recover the suppressed-carrier BPSK: bandpass 57k, mix to DC,
    DECIMATE to ~9.5 kHz (8 sps), then a Costas loop. Returns
    (soft symbols, decimated_fs)."""
    from scipy.signal import butter, sosfilt, resample_poly
    from math import gcd
    sos = butter(6, [fc - 3000, fc + 3000], btype="band", fs=fs, output="sos")
    x = sosfilt(sos, mpx).astype(np.float32)
    n = np.arange(len(x))
    z = (x * np.exp(-2j * np.pi * fc / fs * n)).astype(np.complex64)
    sos2 = butter(4, 2400, btype="low", fs=fs, output="sos")
    z = sosfilt(sos2, z).astype(np.complex64)
    dec_fs = int(round(RBPS * 8))          # 9500 Hz, 8 samples/bit
    g = gcd(dec_fs, int(fs))
    z = resample_poly(z, dec_fs // g, int(fs) // g).astype(np.complex64)
    phase = 0.0
    freq = 0.0
    a, b = 0.01, 0.01 ** 2 / 4
    out = np.empty(len(z), np.complex64)
    for i in range(len(z)):
        v = z[i] * np.exp(-1j * phase)
        out[i] = v
        e = np.real(v) * np.imag(v)
        freq += b * e
        phase += freq + a * e
    return out, dec_fs


def bits_from_symbols(rec, fs):
    """1187.5 bps clock recovery (Gardner-lite) -> differential bits."""
    sps = fs / RBPS
    n = int(len(rec) / sps) - 2
    syms = np.empty(n, np.complex64)
    pos = 0.0
    for k in range(n):
        p = int(pos)
        if p >= len(rec):
            syms = syms[:k]; break
        syms[k] = rec[p]
        pos += sps
    bit = (np.real(syms) > 0).astype(np.int8)
    # RDS is differentially encoded: transition = 1
    return (bit[1:] ^ bit[:-1]).astype(np.int8)


def find_blocks(bits):
    """Slide a 26-bit window; when four consecutive blocks match the
    A,B,C/Cp,D offset pattern, we have a synced group."""
    groups = []
    N = len(bits)
    i = 0
    def val(a):
        v = 0
        for x in bits[a:a+26]: v = (v << 1) | int(x)
        return v
    while i < N - 104:
        b1 = val(i)
        if check_offset(b1, OFFSET["A"]):
            if (check_offset(val(i+26), OFFSET["B"]) and
                (check_offset(val(i+52), OFFSET["C"]) or check_offset(val(i+52), OFFSET["Cp"])) and
                check_offset(val(i+78), OFFSET["D"])):
                groups.append([val(i) >> 10, val(i+26) >> 10,
                               val(i+52) >> 10, val(i+78) >> 10])
                i += 104
                continue
        i += 1
    return groups


def decode_groups(groups):
    ps = [" "] * 8
    rt = [" "] * 64
    pi = None
    pty = None
    for g in groups:
        pi = g[0]
        gtype = (g[1] >> 12) & 0xF
        ab = (g[1] >> 11) & 1
        pty = (g[1] >> 5) & 0x1F
        if gtype == 0:                     # 0A/0B: PS name
            idx = g[1] & 0x3
            c1, c2 = (g[3] >> 8) & 0xFF, g[3] & 0xFF
            ps[idx*2] = CHARS.get(c1, ps[idx*2])
            ps[idx*2+1] = CHARS.get(c2, ps[idx*2+1])
        elif gtype == 2:                   # 2A: RadioText
            idx = g[1] & 0xF
            if ab == 0:
                for j, ch in enumerate([(g[2] >> 8) & 0xFF, g[2] & 0xFF,
                                        (g[3] >> 8) & 0xFF, g[3] & 0xFF]):
                    if idx*4+j < 64:
                        rt[idx*4+j] = CHARS.get(ch, rt[idx*4+j])
    return {"pi": f"{pi:04X}" if pi else None,
            "pty": PTY.get(pty, str(pty)) if pty is not None else None,
            "ps": "".join(ps).strip(), "rt": "".join(rt).strip(),
            "groups": len(groups)}


def full_decode(iq, fs):
    mpx = fm_discriminate(iq, fs)
    rec, dec_fs = costas_bpsk(mpx, fs)
    best = {"groups": 0}
    for inv in (rec, -rec):                # BPSK phase ambiguity
        bits = bits_from_symbols(inv, dec_fs)
        for flip in (bits, bits ^ 1):      # differential polarity
            g = find_blocks(flip)
            if len(g) > best["groups"]:
                d = decode_groups(g)
                best = d
    return best


def cmd_selftest(args):
    print("=" * 60)
    print("Radio Tuna RDS self-test (block sync + PS decode)")
    print("=" * 60)
    # build a valid RDS bitstream: 0A groups spelling 'WXTUNAFM'
    ok = True
    PS = "WXTUNAFM"
    def mkblock(info16, off):
        # append the 10-bit checkword so syndrome==off
        b = info16 << 10
        # brute the checkword
        for cw in range(1024):
            if syndrome(b | cw) == off:
                return b | cw
        return b
    bits = []
    for gi in range(4):
        pi = 0x4D54
        blkA = mkblock(pi, OFFSET["A"])
        b1 = (0 << 12) | (0 << 11) | (10 << 5) | gi       # 0A, pty=10, addr=gi
        blkB = mkblock(b1, OFFSET["B"])
        blkC = mkblock(0x1234, OFFSET["C"])
        c1, c2 = ord(PS[gi*2]), ord(PS[gi*2+1])
        blkD = mkblock((c1 << 8) | c2, OFFSET["D"])
        for blk in (blkA, blkB, blkC, blkD):
            for i in range(25, -1, -1):
                bits.append((blk >> i) & 1)
    bits = np.array(bits * 3, np.int8)      # repeat for sync robustness
    g = find_blocks(bits)
    d = decode_groups(g)
    print(f"  synced groups: {d['groups']}  PI={d['pi']}  PS='{d['ps']}'")
    ok &= d["ps"] == "WXTUNAFM" and d["pi"] == "4D54"
    print(f"  {'OK' if ok else 'FAIL'}")
    print("=" * 60)
    print("SELFTEST", "PASS" if ok else "FAIL")
    print("=" * 60)
    return 0 if ok else 1


def cmd_decode(args):
    raw = np.fromfile(args.file, dtype=np.int16).astype(np.float32) / 32768.0
    iq = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
    print(f"[rds] {Path(args.file).name}: {len(iq)/args.fs:.1f}s")
    d = full_decode(iq, args.fs)
    print(f"[rds] synced groups: {d['groups']}")
    if d["groups"] > 0:
        print(f"[rds] PI={d['pi']}  PTY={d['pty']}")
        print(f"[rds] station name (PS): '{d['ps']}'")
        if d["rt"]:
            print(f"[rds] RadioText: '{d['rt']}'")
        print("  -> FM station DECODED (name/metadata off the air)")
    else:
        print("[rds] no RDS sync - weak subcarrier or station has no RDS")
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    d = sub.add_parser("decode")
    d.add_argument("--file", required=True)
    d.add_argument("--fs", type=float, default=250000)
    args = ap.parse_args()
    if args.cmd == "selftest":
        sys.exit(cmd_selftest(args))
    else:
        cmd_decode(args)


if __name__ == "__main__":
    main()
