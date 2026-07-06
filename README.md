# gr-radiotuna 🐟📻

**Adaptive radio decoding — the TV Tuna method, unleashed on every band.**

Born 2026-07-05 from [Software-TV-Tuner](https://github.com/Felbs/Software-TV-Tuner)
(TV Tuna), where the method was forged against the hardest teacher in
broadcast: ATSC television on marginal antennas.

## The method
Every digital radio decoder secretly knows how well it's doing — a MER,
a BER, a CNR, a CRC rate. Consumer receivers ignore that voice and treat
reception as take-it-or-leave-it. **Adaptive decoding closes the loop**:

1. Find the decoder's continuous truth-dial and surface it live
2. Hill-climb everything against it — gain, frequency, antenna aim
   (by tone and voice), timing, schedule
3. Recalibrate forever — every config goes stale; the product is the
   loop, not the settings
4. Demand liveness proof — a metric without decoded content is a mirage

## Campaign 1 — HD Radio (NRSC-5)
Digital audio hiding ~20 dB beneath every big FM station; marginal by
design; `nrsc5` provides the dial. See `docs/HD_CAMPAIGN.md`.
Tooling so far: `tools/hd_radio.py` (capture / decode / live listening
via SDRplay → decimation → nrsc5).

## The map
Weather satellites (Meteor LRPT, GOES HRIT dish-aiming), AIS ship
tracking, aviation weather, ionospheric FT8/WSPR, trunked P25 — ranked
with rationale in `docs/ROADMAP.md`.

## Lineage & shape
GNU Radio out-of-tree module lineage (sibling of gr-atscplus in the TV
Tuna repo). Decoder families will live under one namespace as blocks:
`radiotuna.hd_*`, `radiotuna.sat_*`, `radiotuna.ais_*` — whimsy on the
marquee, discipline in the API.
