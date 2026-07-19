# The Science — radio physics, math, and the logic behind the code

This is the "why" file. Like the Software-TV-Tuner science deck, every
mechanism we ship is here with the physics that motivated it and the
measurement that proved it. The house method throughout:

> **Find the signal's known-in-advance structure, turn its error into a
> live truth dial, and hill-climb everything against that dial.**
> Never trust the ear or the eye when a meter can vote. Build the
> synthetic transmitter first. Let a referee decoder check your work.

---

## 1. The anatomy of broadcast FM (what's inside "one station")

An FM station modulates a single carrier with a **composite baseband**
signal — a frequency-multiplexed stack:

```
   0 ──────── 15 kHz   M = (L+R)/2      the mono audio everyone hears
  19 kHz               pilot tone       9% deviation, the phase reference
  23 ─────── 53 kHz    (L−R)/2 DSB-SC   stereo difference, on 38 kHz = 2x pilot
  57 kHz               RDS              1187.5 bps BPSK, station data
  67 / 92 kHz          SCA leases       (sometimes) private subcarriers
```

The composite frequency-modulates the carrier with ±75 kHz peak
deviation. **Everything a receiver needs to know is in that map** — the
pilot is a pure known tone, the stereo subcarrier is exactly twice it,
phase-locked. Known structure = free truth dials.

## 2. FM noise physics: why hiss lives up high

After an FM discriminator, white RF noise is **not** white in the
audio: its density rises as **f²** (the discriminator differentiates
phase, and differentiation multiplies noise by frequency). Twice the
composite frequency = 4x the noise power density. This single parabola
explains most of what we fixed:

- **The v1 hiss bug**: the old demod wrote the *whole* 0–46 kHz
  discriminator output into the audio file with no 15 kHz low-pass.
  On the f² parabola, the 15–46 kHz region — pure noise, no program —
  carried **−25 dB relative to the audio band** of delivered hiss.
  The v2 15 kHz FIRs put that at −60 dB. You can't hear a filter that
  isn't there; you *can* hear the noise it would have removed.
- **De-emphasis (75 µs)**: broadcasters pre-boost treble; receivers
  cut it back with a one-pole low-pass, which also cuts the top of the
  noise parabola. Standard since the 1940s because the parabola was
  understood in the 1930s.
- **The stereo noise penalty**: the (L−R) subcarrier sits at 23–53 kHz,
  high on the parabola. Demodulating it folds that noise into the
  audible band. Integrate f² across the bands and the difference
  channel carries roughly **16 dB more noise** than the mono channel.
  Stereo is a luxury of strong signals — which is *the whole logic* of
  the **SNR-adaptive stereo blend**: full stereo above ~20 dB pilot
  SNR, gliding to mono by ~6 dB. The blend knob IS the hiss knob.

## 3. Channel selection: your neighbor is your noise

A discriminator answers "what is the instantaneous frequency of
*everything I can see*." Feed it ±744 kHz of spectrum and a neighboring
station 400 kHz away is *inside the question* — the two carriers beat,
and the composite floods with wideband noise even when your station's
RF is strong.

Bench proof (2026-07-19): 107.5 and 99.7 arrived with strong RF
(+33/+25 dB in-channel) but decoded to a drowned composite; 90.9's
pilot SNR jumped **7.8 → 28.7 dB** the moment a ±120 kHz channel-select
FIR went in front of the discriminator. Carson's rule sizes the
channel: BW ≈ 2(Δf + f_max) = 2(75 + 53) = 256 kHz — so ±120 kHz
passband at a 297.675 kHz processing rate fits exactly.

**Law: strong-RF-but-noisy-FM ⇒ suspect the neighbors first.**

## 4. Pilot doubling: stereo without a PLL

The 38 kHz stereo subcarrier is transmitted *suppressed* — there is
nothing at 38 kHz to lock onto. But the 19 kHz pilot is its
phase-locked half. Squaring a unit phasor doubles its phase:

```
p̂ = pilot as a complex unit phasor        (mix @19 kHz + narrow LPF)
r₃₈ = p̂²                                   exact 38 kHz reference,
                                            tuner offset and all
(L−R) = 2 · LPF₁₅ᵏ( composite · conj(r₃₈) )
```

No PLL, no loop dynamics — the reference *is* the pilot, so it tracks
station offset and our tuner's ppm error for free.

**The 90° lesson.** Whether (L−R) lands in the real or imaginary part
of that product depends on the sin/cos phase convention between pilot
and subcarrier — get it wrong and stereo silently nulls while every
other meter reads perfect. We measured instead of assuming: on a live
music station the imaginary branch held −6.3 dB (rel M) of coherent
stereo, the real branch −30.8 dB of residue. The code now carries a
**synthetic-transmitter selftest** (`fm_stereo.py --selftest`: L=1 kHz,
R=3 kHz, textbook composite) that proves 33/26 dB channel separation
and the L/R polarity on every change. *Build the transmitter first.*

## 5. Honest meters: the probe-filter trap and ENBW

The pilot-SNR dial compares pilot power against a noise probe at
21.3 kHz (a guard band with no program content). Two design laws paid
for in blood:

- **Skirt leakage caps the dial.** A single one-pole low-pass at
  500 Hz attenuates the (huge) pilot only −13 dB at the 2.3 kHz probe
  spacing — the "noise" probe was mostly pilot, clamping every reading
  near 13 dB. Cascading three poles puts the skirt at −40 dB. If a
  dial saturates at a suspiciously round number, check the skirts.
- **Noise bandwidth is computed, not assumed.** Converting probe power
  to a density needs the filter's equivalent noise bandwidth; for the
  cascade we integrate |H(f)|² numerically rather than quote a
  textbook single-pole formula. The audio-SNR dial then projects the
  measured density up the f² parabola across 0–15 kHz:
  N_audio = N₀/f_probe² · (15 kHz)³/3.

## 6. The anti-regression discipline

Every invention is guilty until measured innocent, at scale:

- **Referee decoding**: antenna/station quality verdicts come from the
  *stock* decoder, so our own knobs can't flatter the data they're
  judged by.
- **Field certification beats spot checks**: the FM knob campaign's
  lesson was that small A/Bs miss regressions. The all-day lab decodes
  a cliff-grade specimen **every slot, all day**, three ways (stock vs
  ALBACORE=1 vs +auto), scored by real audio seconds, and flags any
  slot where the knobs lose to stock.
- **Replay can never validate a live promotion** (the 7/10 TV law):
  offline wins are hypotheses; the live chain has deadlines and
  overflow modes replay can't see. Live tests gate final promotion.

## 7. The three-antenna experiment (2026-07-19 design)

Hardware: one RSPdx, three software-selectable antennas —
A = rabbit ears, B = "Old Faithful" TV yagi, C = roof discone.
They differ in gain pattern, polarization, height, and feedline — so
"which antenna" is an empirical function of station, band, and time.

- **Q1 (winner map)**: every 25-min slot captures every station on
  every antenna; each specimen yields HD metrics (sync/MER/BER/audio
  seconds via referee) *and* analog metrics (pilot SNR / audio SNR via
  fm_stereo) — one capture, two sciences. Result: an
  antenna × station × time quality cube.
- **Q2 (gain response)**: a rotating mini IFGR sweep (center ± 4 dB on
  one grid cell per slot) accumulates response curves for every
  (antenna, station) pair by evening — the data a "perfect tune"
  algorithm needs, measured, not guessed.
- **Q3 (perfect tune)**: fit the cube: pick antenna + gains per
  station; validate the picks against the fixed-antenna baseline
  before shipping them as defaults.
- **Controls**: a fixed RFI probe (same antenna, same gains, every
  slot) separates environment drift from rig changes; station order
  rotates each slot so time-of-day can't masquerade as a station
  effect; raw RF facts (level, in-channel SNR) are logged per capture
  so RF-side changes separate from decode-side changes.

*Lab scripts live in the albacore repo (`lab/hd_day_lab2.py`); the
demod under test is this repo's `tools/fm_stereo.py`.*
