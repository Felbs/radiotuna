r"""fm_stereo.py — analog FM done right: the hiss cure.

The v1 panel path (mono, no audio filter) writes the whole 0-46 kHz
discriminator output into the WAV; FM noise rises as f^2, so everything
above the 15 kHz audio band is pure delivered hiss. This module is the
v2 chain, built chunk-streamable so the live panel and offline benches
run the *same* code:

    channel FIR +-120 kHz, decim /5 @ 1.488 MHz -> 297.675 kHz
      (v1 had NO channel filter: the discriminator ate the full
       +-744 kHz, so any neighbor station in that window flooded the
       composite with noise — bench-proven on 107.5/99.7, where strong
       RF decoded to a drowned composite until this filter went in)
      -> discriminator -> composite @ 297.675 kHz
           |-> FIR 15 kHz                        -> M  (L+R)
           |-> pilot mix 19 kHz + 500 Hz 3-pole  -> p  (lock + SNR dial)
           |-> noise mix 21.3 kHz (guard band)   -> n  (the hiss meter)
           |-> c * mix38(p-locked), FIR 15 kHz   -> S  (L-R)
      -> decim /6 -> 49.6 kHz audio, 75 us de-emphasis, AGC
      -> L = M + b*S, R = M - b*S   with b = SNR-adaptive stereo blend

The blend IS the hiss knob: stereo triples noise bandwidth, so at low
pilot SNR we glide toward mono instead of hissing in stereo. All dials
(pilot SNR, audio SNR, blend, AGC) stream out per chunk for the panel's
stats-for-nerds.

Bench mode (offline A/B on wideband captures, SDR not needed):
    python fm_stereo.py --iq lab\hd_cliff\hdfield_93.3_*.cs16 --v1
writes v1/v2 WAVs to lab\out\fm_ab\ and prints the meters.
"""
import numpy as np
from scipy.signal import firwin, lfilter

FS_RAW = 2_976_750.0          # SDR capture rate (2x nrsc5 native)
FS_IN = FS_RAW / 2            # after decimate2 -> channel-filter rate
D0 = 5                        # channel decimation
FSC = FS_IN / D0              # 297,675 Hz: channel + composite rate
D2 = 6                        # audio decimation
FS_AUDIO = int(FSC / D2)      # 49,612 Hz (10 ppm rate lie, inaudible)

F_PILOT = 19_000.0
F_NOISE = 21_300.0            # guard band: above pilot, below 23k DSBSC


def decimate2_cs16(raw):
    """interleaved int16 IQ at FS_RAW -> cs16 at FS_IN (2-tap average)."""
    raw = raw[: (len(raw) // 4) * 4]
    i = raw[0::2].astype(np.int32)
    q = raw[1::2].astype(np.int32)
    i2 = ((i[0::2] + i[1::2]) // 2).astype(np.int16)
    q2 = ((q[0::2] + q[1::2]) // 2).astype(np.int16)
    out = np.empty(2 * len(i2), np.int16)
    out[0::2] = i2
    out[1::2] = q2
    return out


class FMStereo:
    """Chunk-streaming stereo FM demod with truth dials.

    feed() takes interleaved cs16 at FS_IN (what decimate2 emits) and
    returns (stereo_int16_interleaved, telemetry_dict)."""

    def __init__(self, stereo=True):
        self.stereo = stereo
        # channel-select FIR: +-120 kHz passband (Carson BW ~256 kHz fits
        # the 297.675 kHz output rate), neighbors outside it are GONE
        self.b_chan = firwin(91, 120_000, fs=FS_IN)
        self.zi_chan = np.zeros(len(self.b_chan) - 1, np.complex128)
        self.ph_chan = 0            # decimation phase carry
        # audio-band FIRs at composite rate (shared design, separate state)
        self.b_aud = firwin(305, 15_000, fs=FSC).astype(np.float32)
        self.zi_m = np.zeros(len(self.b_aud) - 1, np.float32)
        self.zi_s = np.zeros(len(self.b_aud) - 1, np.float32)
        self.ph_m = 0
        self.ph_s = 0
        # pilot/noise probes: complex mix + 500 Hz one-pole CASCADED 3x.
        # A single pole leaks the (huge) pilot into the noise probe only
        # -13 dB down at the 2.3 kHz spacing, capping the readable SNR at
        # ~13 dB; three poles put the skirt at -40 dB (bench-proven).
        a1 = float(np.exp(-2 * np.pi * 500.0 / FSC))
        self.b_p, self.a_p = np.array([1 - a1]), np.array([1.0, -a1])
        self.zi_p = [np.zeros(1, np.complex128) for _ in range(3)]
        self.zi_n = [np.zeros(1, np.complex128) for _ in range(3)]
        # noise bandwidth of the cascade, computed honestly
        f = np.linspace(0, FSC / 2, 20000)
        h1 = (1 - a1) / np.abs(1 - a1 * np.exp(-2j * np.pi * f / FSC))
        h2 = h1 ** 3
        self.enbw = float(np.trapezoid(h2 ** 2, f) / (h2[0] ** 2))
        self.th19 = 0.0             # mixer phase accumulators
        self.th21 = 0.0
        # de-emphasis (75 us) at audio rate, per matrix path
        al = 1.0 - float(np.exp(-1.0 / (75e-6 * FS_AUDIO)))
        self.b_de, self.a_de = np.array([al]), np.array([1.0, al - 1.0])
        self.zi_dem = np.zeros(1, np.float64)
        self.zi_des = np.zeros(1, np.float64)
        self.prev = np.complex64(1 + 0j)
        self.agc = 60000.0
        self.agc_freeze = False    # benches freeze the servo: its slow
                                   # wobble AMs the audio and reads as a
                                   # CNR-independent noise floor
        self.blend = 0.0
        self.snr_ema = None
        self.tele = {}
        # optional composite tap: a consumer (live RDS decoding) sets
        # tap_secs > 0 and reads self.tap (float32 chunks @ FSC)
        self.tap_secs = 0.0
        self.tap = []
        self._tap_n = 0

    def feed(self, raw_cs16):
        x = (raw_cs16[0::2].astype(np.float64)
             + 1j * raw_cs16[1::2].astype(np.float64))
        if len(x) < 64:
            return np.empty(0, np.int16), self.tele

        # channel select + decimate, THEN discriminate
        y, self.zi_chan = lfilter(self.b_chan, 1.0, x, zi=self.zi_chan)
        xc = y[self.ph_chan::D0]
        self.ph_chan = (self.ph_chan - len(y)) % D0
        if len(xc) == 0:
            return np.empty(0, np.int16), self.tele
        xd = np.empty(len(xc) + 1, np.complex128)
        xd[0] = self.prev
        xd[1:] = xc
        self.prev = xc[-1]
        c = np.angle(xd[1:] * np.conj(xd[:-1]))
        n = len(c)
        if self.tap_secs > 0:
            self.tap.append(c.astype(np.float32))
            self._tap_n += n
            while self.tap and \
                    self._tap_n - len(self.tap[0]) > self.tap_secs * FSC:
                self._tap_n -= len(self.tap.pop(0))

        # pilot + noise probes (phase-continuous mixers)
        w19 = 2 * np.pi * F_PILOT / FSC
        w21 = 2 * np.pi * F_NOISE / FSC
        k = np.arange(1, n + 1)
        mix19 = np.exp(-1j * (self.th19 + w19 * k))
        mix21 = np.exp(-1j * (self.th21 + w21 * k))
        self.th19 = float((self.th19 + w19 * n) % (2 * np.pi))
        self.th21 = float((self.th21 + w21 * n) % (2 * np.pi))
        p = c * mix19
        for s in range(3):
            p, self.zi_p[s] = lfilter(self.b_p, self.a_p, p,
                                      zi=self.zi_p[s])
        nz = c * mix21
        for s in range(3):
            nz, self.zi_n[s] = lfilter(self.b_p, self.a_p, nz,
                                       zi=self.zi_n[s])
        p_pow = float((np.abs(p) ** 2).mean())
        n_pow = float((np.abs(nz) ** 2).mean()) + 1e-12
        snr = 10 * np.log10(p_pow / n_pow + 1e-12)
        self.snr_ema = snr if self.snr_ema is None else \
            0.8 * self.snr_ema + 0.2 * snr

        # M (L+R)
        m_c, self.zi_m = lfilter(self.b_aud, 1.0, c, zi=self.zi_m)

        # S (L-R): re-lock 38 kHz from the pilot (phase doubling — carries
        # station offset AND our tuner ppm for free)
        if self.stereo:
            u = p / (np.abs(p) + 1e-9)
            # 38k reference = (pilot phasor)^2; its conjugate mixes the
            # DSBSC subcarrier to baseband. The subcarrier is sin-phased
            # relative to the sin pilot (broadcast standard), so L-R lands
            # in the IMAGINARY part — bench-measured on 93.3: -6.3 dB rel M
            # in Im vs -30.8 in Re (a 90-deg convention bug hid here once).
            s_bb = (c * (mix19 ** 2) * np.conj(u) ** 2).imag * 2.0
            s_c, self.zi_s = lfilter(self.b_aud, 1.0, s_bb, zi=self.zi_s)
        else:
            s_c = np.zeros_like(m_c)

        # audio decimation
        m_a = m_c[self.ph_m::D2]
        self.ph_m = (self.ph_m - n) % D2
        s_a = s_c[self.ph_s::D2]
        self.ph_s = (self.ph_s - n) % D2
        la = min(len(m_a), len(s_a))
        m_a, s_a = m_a[:la], s_a[:la]
        if la == 0:
            return np.empty(0, np.int16), self.tele

        # de-emphasis
        m_a, self.zi_dem = lfilter(self.b_de, self.a_de, m_a, zi=self.zi_dem)
        s_a, self.zi_des = lfilter(self.b_de, self.a_de, s_a, zi=self.zi_des)

        # SNR-adaptive stereo blend: full stereo at 20 dB pilot SNR,
        # full mono at 6 dB — the hiss knob
        target = float(np.clip((self.snr_ema - 6.0) / 14.0, 0.0, 1.0))
        if not self.stereo:
            target = 0.0
        self.blend += 0.15 * (target - self.blend)

        # AGC on the mono core (same feel as v1)
        if not self.agc_freeze:
            r = float(np.sqrt((m_a ** 2).mean())) + 1e-9
            want = min(max(5500.0 / r, 12000.0), 400000.0)
            self.agc += 0.06 * (want - self.agc)

        L = np.clip((m_a + self.blend * s_a) * self.agc, -32000, 32000)
        R = np.clip((m_a - self.blend * s_a) * self.agc, -32000, 32000)
        pcm = np.empty(2 * la, np.int16)
        pcm[0::2] = L.astype(np.int16)
        pcm[1::2] = R.astype(np.int16)

        # audio SNR dial: project f^2 noise density from the probe into
        # the 0-15k audio band (pre-de-emphasis, honest composite units)
        n0 = n_pow / self.enbw / (F_NOISE ** 2)      # density / f^2
        n_aud = n0 * (15_000.0 ** 3) / 3
        p_m = float((m_c ** 2).mean())
        self.tele = {
            "pilot_snr_db": round(self.snr_ema, 1),
            "stereo_blend": round(self.blend, 2),
            "audio_snr_db": round(10 * np.log10(p_m / (n_aud + 1e-15)
                                                + 1e-12), 1),
            "agc_db": round(20 * np.log10(self.agc / 60000.0), 1),
            "fm_mode": ("stereo" if self.blend > 0.5 else
                        "blend" if self.blend > 0.05 else "mono"),
        }
        return pcm, self.tele


def wav_header(fs, channels):
    import struct
    return (b"RIFF" + struct.pack("<I", 0x7FFFFFF0) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, fs,
                                    fs * 2 * channels, 2 * channels, 16)
            + b"data" + struct.pack("<I", 0x7FFFFF00))


def selftest():
    """Synthetic transmitter (the in-vitro law): textbook stereo composite
    -> FM modulate -> cs16 -> FMStereo. L carries 1 kHz, R carries 3 kHz;
    separation proves the pilot-doubling phase AND the L/R polarity."""
    fs = FS_RAW
    t = np.arange(int(fs * 4.0)) / fs
    Lt = 0.8 * np.sin(2 * np.pi * 1000 * t)
    Rt = 0.8 * np.sin(2 * np.pi * 3000 * t)
    comp = (0.45 * (Lt + Rt) + 0.09 * np.sin(2 * np.pi * 19000 * t)
            + 0.45 * (Lt - Rt) * np.sin(2 * np.pi * 38000 * t))
    ph = np.cumsum(2 * np.pi * 75_000 * comp / fs)
    iq = 8000 * np.exp(1j * ph)
    # white noise floor so the SNR dials have something honest to read
    iq += (np.random.default_rng(7).normal(0, 30, (len(iq), 2))
           .view(np.complex128).ravel())
    raw = np.empty(2 * len(iq), np.int16)
    raw[0::2] = iq.real.astype(np.int16)
    raw[1::2] = iq.imag.astype(np.int16)
    dem = FMStereo()
    outs = []
    tele = {}
    for i in range(0, len(raw), 2 * 262144):
        pcm, tele = dem.feed(decimate2_cs16(raw[i:i + 2 * 262144]))
        outs.append(pcm)
    pcm = np.concatenate(outs)[2 * FS_AUDIO:]     # skip filter settle
    L = pcm[0::2].astype(np.float64)
    R = pcm[1::2].astype(np.float64)

    def tone(x, f):
        n = len(x)
        w = np.exp(-2j * np.pi * f * np.arange(n) / FS_AUDIO)
        return np.abs((x * w).mean()) ** 2

    sep_L = 10 * np.log10(tone(L, 1000) / (tone(L, 3000) + 1e-9))
    sep_R = 10 * np.log10(tone(R, 3000) / (tone(R, 1000) + 1e-9))
    print(f"selftest: {tele}")
    print(f"  L wants 1k: {sep_L:+.1f} dB   R wants 3k: {sep_R:+.1f} dB "
          f"(>=20 dB = stereo demod + polarity PROVEN)")
    assert sep_L > 20 and sep_R > 20, "stereo separation FAILED"
    print("  PASS")


# ── bench: offline A/B on a wideband capture ────────────────────────────
def _v1_mono(raw_cs16, state):
    """The old panel path, verbatim (boxcar 16, no LPF) for the A/B."""
    x = (raw_cs16[0::2].astype(np.float32)
         + 1j * raw_cs16[1::2].astype(np.float32))
    xd = np.empty(len(x) + 1, np.complex64)
    xd[0] = state["prev"]
    xd[1:] = x
    state["prev"] = x[-1]
    d = np.angle(xd[1:] * np.conj(xd[:-1]))
    k = (len(d) // 16) * 16
    a = d[:k].reshape(-1, 16).mean(axis=1)
    al = 1.0 / (1.0 + 75e-6 * 93023)
    out, state["zi"] = lfilter(np.array([al]), np.array([1.0, al - 1.0]),
                               a.astype(np.float64), zi=state["zi"])
    r = float(np.sqrt((out ** 2).mean())) + 1e-9
    want = min(max(5500.0 / r, 12000.0), 400000.0)
    state["agc"] += 0.06 * (want - state["agc"])
    return np.clip(out * state["agc"], -32000, 32000).astype(np.int16)


def bench(iq_path, out_dir, secs=30.0, v1=False):
    import glob
    from pathlib import Path
    path = Path(sorted(glob.glob(str(iq_path)))[-1])
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dem = FMStereo()
    fv2 = open(out_dir / (path.stem + "_v2stereo.wav"), "wb")
    fv2.write(wav_header(FS_AUDIO, 2))
    fv1 = None
    v1s = {"prev": np.complex64(1 + 0j), "zi": np.zeros(1), "agc": 60000.0}
    if v1:
        fv1 = open(out_dir / (path.stem + "_v1mono.wav"), "wb")
        fv1.write(wav_header(93023, 1))
    n_want = int(secs * FS_RAW) * 2
    read = 0
    tele = {}
    with open(path, "rb") as f:
        while read < n_want:
            raw = np.fromfile(f, dtype=np.int16, count=2 * 262144)
            if len(raw) < 4:
                break
            read += len(raw)
            cs = decimate2_cs16(raw)
            pcm, tele = dem.feed(cs)
            fv2.write(pcm.tobytes())
            if fv1:
                fv1.write(_v1_mono(cs, v1s).tobytes())
    fv2.close()
    if fv1:
        fv1.close()
    print(f"{path.name}: {tele}")
    return tele


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--iq", help="cs16 capture at 2.97675 MS/s")
    ap.add_argument("--secs", type=float, default=30)
    ap.add_argument("--out", default=r"Z:\src\gr-radiotuna\lab\out\fm_ab")
    ap.add_argument("--v1", action="store_true", help="also write v1 mono")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        bench(a.iq, a.out, a.secs, a.v1)
