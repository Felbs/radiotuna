# Radio Tuna — Campaign 1: HD Radio (NRSC-5)
*Drafted 2026-07-05 evening. The adaptive-tuning method, ported from
television to the digital signal hiding inside FM broadcasts.*

## Why HD Radio first
- Same disease we just beat: digital carriers ~-20 dB under the analog
  host; reception is marginal by DESIGN. Adaptive tuning has real work.
- The discone is vertically polarized — wrong for TV, ideal for FM.
  June sweep: 12/12 FM stations strong on it. Our "worst TV antenna"
  is our best radio antenna; nothing new to buy or mount.
- `nrsc5` (open source) decodes it AND exposes the truth-metrics we
  need: sync acquisition, MER-like CNR, BER per logical channel.
- Payoff is immediate and demo-friendly: hidden HD2/HD3 subchannels,
  song/artist metadata, album art bytes.

## The method transfer (TV concept → HD equivalent)
| TV Tuna                     | Radio Tuna HD                          |
|-----------------------------|----------------------------------------|
| MER from equalizer          | nrsc5 CNR + BER as the live dial       |
| 15.2 dB cliff               | sync/no-sync + BER waterfall           |
| channel scan (RF 2-36)      | FM band sweep 88-108, +HD flag per stn |
| gain calibration grid       | identical (SDRplay knobs unchanged)    |
| aiming tone / signal finder | identical, driven by CNR/BER           |
| flight recorder             | identical                              |
| tower grid + % meters       | station grid + HD subchannel buttons   |
| forced-video cliff mode     | MPS/SPS fallback (HD1 dies last)       |

## Plumbing plan
1. **Decoder**: nrsc5 (github.com/theori-io/nrsc5). Native input is
   RTL-SDR or IQ FILE (-r). SDRplay isn't supported directly → feed it
   our IQ: capture/pipe at 1,488,375 Hz (its native rate) or resample
   8 MS/s ÷ 5.375... → cleaner to capture at 2.976750 MS/s and
   decimate ÷2, or capture 1.4884 MS/s directly (RSPdx supports
   arbitrary rates ≥ 2 MS/s best; below 2 MS/s the RSP resamples
   internally — verify quality both ways in experiment 1).
2. **Format**: nrsc5 -r expects cu8 or cs16 depending on build — pin
   at first run; our iq_capture already writes cs16.
3. **Live loop later**: named pipe from a Soapy reader into nrsc5
   (same tail-pipe pattern as the TV extractor).

## Experiment ladder
- **E1 — first light (offline)**: capture 10 s of WTOP 103.5 on the
  discone, feed the file to nrsc5, look for MPS audio + sync stats.
  Pass = the decoder chain works end-to-end on our hardware.
- **E2 — the dial**: parse nrsc5's stderr (CNR, BER lines) into our
  meter format; SIGNAL FINDER gets an "HD" mode (tone follows BER
  inverted). Aim the discone; watch a below-threshold station sync.
- **E3 — gain cal**: mer_gain_cal fork scoring nrsc5 BER instead of
  fs_err — find each station's front-end sweet spot.
- **E4 — band survey**: sweep 88-108, per-station: analog RSSI, HD
  present?, CNR, BER, subchannel list → the "station grid".
- **E5 — the marginal hunt**: weakest HD station the survey finds →
  full adaptive treatment (cal + aim + time-of-day watch) → decode a
  station "impossible" on consumer gear. That's the Radio Tuna
  proof-of-concept, mirroring the rabbit-ears WETA moment.

## Open items
- nrsc5 Windows binary: official repo ships source + CI builds; needs
  a download-and-verify step (OWNER APPROVAL before fetching).
- SDRplay native support exists in some nrsc5 forks (nrsc5-dui,
  SoapySDR patches) — evaluate after E1 proves the file path.
- Legal note: receive-only, zero restrictions.
