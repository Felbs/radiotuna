# Radio Tuna: the next level — verified cross-project roadmap (2026-07-19)

Deep-research sweep over the whole Tuna family: 106 agents, 21+ sources,
adversarial 3-vote verification. Every dB figure below is **simulation unless
marked otherwise** — per family law, nothing is believed until it survives our
replay corpus + live overflow-gated A/B.

## Tier 1 — measured in literature, CPU-feasible, direct fit

1. **Close the equalizer↔decoder loop (Software-TV-Tuner).** The single
   largest unexploited technique family-wide. Ladder of entry:
   - *Cheapest*: replace the DFE slicer with a **traceback-depth-1 trellis
     decoder** in the feedback path — ATSC-specific, peer-reviewed: decision
     SER improves >5 dB, **~2 dB at threshold-of-visibility** (ceiling ~3 dB
     = error-propagation-free DFE). ETRI J. 26(2) 2004.
   - *Full*: turbo equalization (Tüchler/Koetter/Singer 2002) — approaches
     the AWGN bound on severe ISI; hard decision feedback is proven
     DETRIMENTAL inside the loop (our decision-directed EQs take note).
   - *Modern*: **expectation-propagation filter turbo eq** (Santos 2018):
     2–5 dB expected for 8-VSB-class alphabets at LMMSE-FIR complexity order.
   - REFUTED en route: "linear MMSE SISO equals MAP in the loop" (0-3) —
     close, not equal.
2. **Koetter-Vardy algebraic soft RS decoding (TV, one rung above our
   GMD/Forney).** ~1.1–1.3 dB over GMD on AWGN — but collapses to ≲0.75 dB
   at ATSC's high-rate RS(207,187). **The real prize: ~3 dB over hard
   decision on fast fading** — aimed exactly at our impulse/fader classes.
3. **Data-aided channel tracking (albacore's 5 Hz wall).** DeepRx (Nokia,
   IEEE TWC 2021) holds genie-LMMSE BER at **500 Hz doppler with one pilot**
   — proof the wall is architecture, not physics. The transferable mechanism
   is *extracting channel state from data symbols themselves*; a classical
   decision-directed iterative estimator captures part of this without any
   neural net. (Our TRACK_FAST experiment already showed block-frozen
   amplitude wasn't the binding constraint — decision-directed phase/gain
   re-estimation is the next rung.)
4. **iBiquity dual-path structures (albacore, needs 2nd tuner for full
   value).** Verified patents: FAC as TWO demod paths (with/without
   cancellation) MRC'd at the soft-decision level, never switched;
   antenna MRC at the Viterbi branch-metric plane, weight a*/σ², ~3 dB AWGN
   and more in fading. Production HD receivers already do dynamic dual-
   sideband quality comparison — the industry baseline we A/B against.

## Open fields (nothing survived verification — frontier or unstudied)
- **Weather/tropo forecasts predicting Knob-of-Time hour-curves** — no
  claims either way. A genuinely novel experiment for this project: regress
  our logged quality curves against public forecast ducting indices.
- **Multi-pass LRPT soft-bit/image combining (wxTuna)** — no literature
  surfaced; our diversity math (quad-combiner) may be the first practical
  take.
- **GP/Bayesian-optimization station tuning** and **physical-metric →
  perceived-quality mapping** — still open (matches the audio-metrics
  research: our LISTEN% + local calibration is frontier work).

## Systematic caveats (the referee's own warnings)
Simulation-only figures; gains shrink in our operating regimes (KV at high-
rate RS; EP turbo's big numbers are for dense QAM); iBiquity's 3 dB is
theoretical array gain. Family law applies: replay-A/B, then live
overflow-gated A/B, then belief.
