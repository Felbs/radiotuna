# RADIO TUNA 🐟📻

**One radio, three decks — FM · AM · Shortwave — adaptive decoding on every band.**

Radio Tuna is the whole-radio project: one web UI (`tools/radio_panel.py`,
`:8643`) with an FM deck (HD Radio via the [albacore](https://github.com/Felbs/albacore)
engine, our instrumented nrsc5 fork), an AM deck (medium-wave scan, live
synchronous-AM listening, HD-AM attempts), and a shortwave deck (world-band
scans identified live against the EiBi schedule — station, language, target,
transmitter site, and a full when-to-listen guide per frequency).

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

**Deep dive: [docs/SCIENCE.md](docs/SCIENCE.md)** — the physics, math,
and logic behind every mechanism in the code: the FM noise parabola and
why the hiss lived above 15 kHz, Carson's rule and the neighbor-station
trap, pilot-doubling stereo (and the 90° convention bug measurement
caught), honest meter design (probe skirts, noise bandwidth), the
anti-regression discipline, and the three-antenna experiment design.

## Quickstart
```bash
git clone https://github.com/Felbs/radiotuna.git
cd radiotuna
python tools/radio_panel.py             # http://localhost:8643 - RADIO TUNA, all three decks
python tools/sync_am.py selftest        # sync-AM + sideband-diversity proofs, no radio
python tools/ais.py selftest            # AIS encode->decode roundtrip, no radio
python tools/broadcast_guide.py survey  # live: FM+AM+SW station survey (~1 min)
python tools/broadcast_guide.py show    # render the guide it found
python tools/radio_room.py              # http://localhost:8645 - click stations, listen
```
**Dependencies:** `numpy`, `scipy`, `numba`, and the `SoapySDR` python
bindings + a driver for your SDR. Easiest path is
[radioconda](https://github.com/ryanvolz/radioconda); on Debian/Ubuntu:
`apt install python3-numpy python3-scipy python3-numba python3-soapysdr soapysdr-module-all`.
Optional externals: [`albacore`](https://github.com/Felbs/albacore) — our
instrumented [nrsc5](https://github.com/theori-io/nrsc5) fork and Radio
Tuna's preferred HD Radio engine (point `ALBACORE_EXE` at it; falls back
to stock `nrsc5` via `NRSC5_EXE`/PATH) — and `mpv` (audio playback,
`MPV_EXE`).
Note: `lab/` (surveys, caches, recordings) is empty on a fresh clone and
fills as the tools run.

## The tools
| Tool | What it does |
|---|---|
| `broadcast_guide.py` | ONE page of everything hearable: FM (+HD names), AM, shortwave named against the EiBi schedule |
| `radio_room.py` | The listening room (`:8645`): every found station clickable, quality-graded audio + truth dials (carrier MER, sideband symmetry, RDS MER) |
| `radio_panel.py` | **RADIO TUNA** (`:8643`) — the three-deck UI: FM/HD survey (albacore engine), AM deck (scan + live sync-AM + HD-AM), SW deck (EiBi-identified world-band scans + schedule guide) |
| `am_listen.py` | Live medium-wave listening: offset-tuned carrier-locked sync-AM, growing-audio playback |
| `hd_radio.py` | HD Radio (NRSC-5) capture / decode / live listen |
| `sw_listen.py` | One-shot AM/shortwave listen: synchronous detection + hum-notch/Wiener rescue chain |
| `sync_am.py` | The sync-AM lab: envelope vs synchronous vs sideband-diversity MRC, with selftest proofs |
| `rds.py` | FM RDS decoder — stations name themselves |
| `hf_knob.py` | The ionosphere clock: FT8 + shortwave band-openness curves, learned hourly |
| `am_night.py` | AM broadcast scanner (the skywave story, night vs day) |
| `ais.py` | AIS ship tracking on 162 MHz (both channels from one capture) |
| `drm.py` | DRM digital-shortwave acquisition |
| `bandscan.py` | 25–1500 MHz classifier with a built-in legality guard (refuses decoders on protected bands by code) |

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
