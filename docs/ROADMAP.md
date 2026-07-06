# Radio Tuna — the full frequency roadmap
*2026-07-05. Where the adaptive method (find the decoder's continuous
truth-dial, hill-climb it, recalibrate forever, demand liveness proof)
pays beyond television — ranked by adaptive-benefit × payoff × fit to
hardware we already own (RSPdx 1 kHz-2 GHz, discone, TV antennas).*

## Tier 1 — campaign material
1. **HD Radio / NRSC-5 (88-108 MHz)** — CHOSEN, see RADIO_TUNA_HD.md.
   Digital -20 dB under analog host; discone is ideal; nrsc5's CNR/BER
   is the dial; consumer radios fail on everything but strong locals.
2. **Meteor-M LRPT weather imagery (137 MHz)** — the classic "everyone
   struggles" signal: the satellite MOVES (Doppler ramps ±3 kHz,
   polarization spins, signal rises/falls through a 12-min pass).
   Viterbi BER is the dial; the adaptive twist nobody does well:
   CONTINUOUS mid-pass retuning (gain + frequency + symbol timing)
   scheduled by a pass-predictor — our sentry concept pointed at the
   sky. NOAA APT (analog) as the warm-up target. Discone workable;
   a $30 V-dipole makes it easy. Payoff: satellite photos we received.
3. **GOES HRIT geostationary weather (1.69 GHz)** — the aiming tone's
   destiny: pointing a dish at an invisible fixed satellite IS the
   signal-finder use case, and the LRIT/HRIT Viterbi stats are the
   dial. Needs ~$130 hardware (grid dish + SAWbird LNA). Full-disk
   Earth photos every 30 min, forever, from a fixed dish.

## Tier 2 — utility decodes, low effort, discone-native
4. **AIS ship tracking (162 MHz)** — Chesapeake/Potomac traffic on the
   discone (vertical, VHF = its home turf). GMSK bursts; per-message
   CRC rate is the dial; adaptive gain stretches range to distant
   ships. Live map of every vessel = strong demo.
5. **UAT aviation weather FIS-B (978 MHz)** — free continental weather
   radar + METARs broadcast for pilots; dump978 decodes; marginal
   ground-station reception is exactly an adaptive-gain problem.
6. **ADS-B (1090 MHz)** — trivially decodable (weak adaptive value);
   worth having only as a range-maximization calibration demo.

## Tier 3 — deeper campaigns for later
7. **HF digital: FT8 / WSPR (1.8-28 MHz)** — the ionosphere is the
   most time-varying channel in radio; WSJT-X reports SNR per decode
   (a per-message dial); adaptive = band/antenna/time selection driven
   by live decode statistics (spot-count hill-climbing). Finally a
   real job for the RSPdx HF port. Global reach on receive alone.
8. **Trunked P25/DMR (450-860 MHz)** — merge target for the
   spectrum-scout side project: SDRTrunk's per-call BER as the dial,
   adaptive gain per talkgroup site. Public-safety monitoring rigor.
9. **DRM shortwave (international broadcast)** — MER dial built into
   the dream decoder; sparse in the Americas; opportunistic.
10. **Inmarsat L-band (1.54 GHz, STD-C/AERO)** — fixed-satellite
    aiming + Es/N0 dial via JAERO/Scytale-C; patch antenna build.

## The pattern (why the method generalizes)
Every entry has the same three ingredients TV had:
  (a) a decoder that KNOWS how well it's doing (BER/CNR/SNR/CRC rate),
  (b) reception that consumer gear treats as take-it-or-leave-it,
  (c) knobs (gain, frequency, antenna, timing, schedule) nobody
      closes the loop on.
Adaptive tuning = closing that loop. TV was the hardest teacher;
everything after is applied coursework.
