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
| `am_best.py` | **The mathematically-best AM/SW demodulator**: carrier filter-lock + sideband-diversity MRC + adaptive bandwidth + hum comb + heterodyne excision + Wiener NR, every stage proven by its own selftest (`am_best.py selftest`) |
| `am_listen.py` | Live medium-wave listening through the best chain, growing-audio playback + live truth dial |
| `am_db.py` | Per-user AM station database: scrapes the FCC's public AM Query into `lab/` on first use — no station data ships in this repo; your panel fills from your own scans (set `RT_QTH=lat,lon` privately for distance-aware idents) |
| `hd_radio.py` | HD Radio (NRSC-5) capture / decode / live listen |
| `sw_listen.py` | One-shot AM/shortwave listen: synchronous detection + hum-notch/Wiener rescue chain |
| `sync_am.py` | The sync-AM lab: envelope vs synchronous vs sideband-diversity MRC, with selftest proofs |
| `rds.py` | FM RDS decoder — stations name themselves |
| `hf_knob.py` | The ionosphere clock: FT8 + shortwave band-openness curves, learned hourly |
| `am_night.py` | AM broadcast scanner (the skywave story, night vs day) |
| `prop_atlas.py` | **Propagation Observatory**: 24/7 AM+SW band-health atlas — half-hourly every-channel quality sweeps, EiBi heard-vs-scheduled join, durable SQLite rollups |
| `ais.py` | AIS ship tracking on 162 MHz (both channels from one capture) |
| `drm.py` | DRM digital-shortwave acquisition |
| `bandscan.py` | 25–1500 MHz classifier with a built-in legality guard (refuses decoders on protected bands by code) |

## Campaign 1 — HD Radio (NRSC-5)
Digital audio hiding ~20 dB beneath every big FM station; marginal by
design; `nrsc5` provides the dial. See `docs/HD_CAMPAIGN.md`.
Tooling so far: `tools/hd_radio.py` (capture / decode / live listening
via SDRplay → decimation → nrsc5).

## The Propagation Observatory
`tools/prop_atlas.py` is the standing band-health atlas: every 30
minutes it takes one 4-second gulp of medium wave plus the 49/41/31/
25/19 m shortwave broadcast bands (about 30 s of radio time, at the
lowest warden priority — it yields to everything and skips the cycle
if the radio is busy), then channelizes each gulp ka9q-style into a
quality grade for **every** channel (~570 of them) and stores the lot
in `lab/prop_atlas.db` (SQLite).

The trick that makes it science rather than a scan log: at insert
time each shortwave row is joined against the EiBi schedule, so every
row records both what was *heard* and who was *scheduled* to be there
at that minute. Heard-vs-scheduled over weeks of half-hour samples is
a propagation observatory — band openings by hour, season, and
frequency, measured from your own antenna. An `hourly` view rolls up
avg quality per channel per UTC hour; `GET /api/atlas` on the panel
serves the latest sweep.

```bash
python tools/prop_atlas.py once     # single test sweep
python tools/prop_atlas.py status   # last sweep, alive counts, top channels
tools/atlas_start.ps1               # start the 24/7 daemon (detached; Windows)
```
(EiBi data is downloaded per-user into `lab/` and never ships with the
repo — see Acknowledgments.)

## The map
Weather satellites (Meteor LRPT, GOES HRIT dish-aiming), AIS ship
tracking, aviation weather, ionospheric FT8/WSPR, trunked P25 — ranked
with rationale in `docs/ROADMAP.md`.

## Lineage & shape
GNU Radio out-of-tree module lineage (sibling of gr-atscplus in the TV
Tuna repo). Decoder families will live under one namespace as blocks:
`radiotuna.hd_*`, `radiotuna.sat_*`, `radiotuna.ais_*` — whimsy on the
marquee, discipline in the API.

## Acknowledgments & prior art
Radio Tuna stands on ideas (and in some cases shoulders-of-giants code)
from the wider SDR community — credit where it's due:

- **[ka9q-radio](https://github.com/ka9q/ka9q-radio)** (Phil Karn,
  KA9Q) — `whole_band.py`'s demodulate-every-channel-at-once approach
  is our independent reimplementation of his fast-convolution
  multichannel architecture. The idea that one wideband capture can be
  *every* station simultaneously is his; go see the original, it's a
  masterwork.
- **[nrsc5](https://github.com/theori-io/nrsc5)** (Theori, with
  **argilo**'s MA3 all-digital AM implementation) — every bit of HD
  Radio decode capability in this project comes from nrsc5. Our
  [albacore](https://github.com/Felbs/albacore) is merely an
  instrumented fork of it: their decoder, our extra dials.
- **[rtl-ml](https://github.com/TrevTron/rtl-ml)** (TrevTron) —
  `mod_classify.py`'s per-carrier signal classification was inspired
  by this project's edge-hardware classifier; ours swaps the trained
  model for explainable physics rules, but the "classify what you
  scanned" idea came from there.
- **[gr-mcp](https://github.com/yoelbassin/gr-mcp)** (yoelbassin) and
  Paul David's GRCon25 *Powering Cognitive Radios with LLMs* —
  `radiotuna_mcp.py` follows the LLM-orchestrates/DSP-stays-
  deterministic framing they articulated for GNU Radio.
- **[faster-whisper](https://github.com/SYSTRAN/faster-whisper)** /
  OpenAI Whisper — the AI EAR's speech recognition; the idea of
  pointing Whisper at demodulated radio audio circulates in the
  DragonOS community.
- **[EiBi](http://www.eibispace.de/)** (Eike Bierwirth) — the
  shortwave schedule database behind every "who's on the air" ident.
- **FCC AM Query** — the public licensing data behind medium-wave
  identification.
