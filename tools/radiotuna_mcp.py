"""radiotuna_mcp.py - Radio Tuna as an MCP server (the gr-mcp borrow).

Exposes the radio to any MCP client (Claude Code, Claude Desktop, local
LLMs) as typed tools. The design keeps our architecture law intact:
the LLM ORCHESTRATES, the deterministic DSP does the samples - every
tool here is a thin call into the running radio_panel (port 8643),
which remains the single owner of the SDR. No panel, no radio: tools
report that honestly instead of fighting for the device.

Run standalone:      python radiotuna_mcp.py
Claude Code config:  see .mcp.json at the repo root
"""
import json
import urllib.request

from fastmcp import FastMCP

PANEL = "http://localhost:8643"
mcp = FastMCP("radiotuna")


def _get(path):
    try:
        with urllib.request.urlopen(PANEL + path, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": f"panel not reachable ({e}) - start "
                         "tools/radio_panel.py first; it owns the SDR"}


def _post(path, body=None):
    try:
        req = urllib.request.Request(
            PANEL + path, data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": f"panel not reachable ({e})"}


@mcp.tool
def band_state() -> dict:
    """Current AM/SW deck state: scanned station grids (with audio-
    quality star ratings and identifications), what's playing, live
    truth-dial quality, scan progress."""
    return _get("/api/band")


@mcp.tool
def scan_am() -> dict:
    """Scan the whole medium-wave band (530-1700 kHz). Stations are
    identified against the user's own scraped FCC database. ~22 s;
    poll band_state for results."""
    return {"result": _post("/api/am/scan")}


@mcp.tool
def scan_sw(band: str = "31m") -> dict:
    """Scan one shortwave broadcast band (49m 41m 31m 25m 22m 19m 16m),
    identified against the EiBi schedule. ~25 s."""
    return {"result": _post("/api/sw/scan", {"band": band})}


@mcp.tool
def scan_sw_all() -> dict:
    """World tour: sweep every shortwave band 49m-16m (~3.5 min)."""
    return {"result": _post("/api/sw/scan_all")}


@mcp.tool
def listen(deck: str, khz: float) -> dict:
    """Tune and LISTEN live (deck 'am' or 'sw') through the best-chain
    demodulator. Audio plays on the host; the live truth dial (0-100
    audio quality, carrier SNR, fades, bandwidth) appears in band_state
    within ~10 s."""
    return {"result": _post("/api/band/listen", {"deck": deck, "khz": khz})}


@mcp.tool
def stop() -> dict:
    """Stop listening / scanning and release the radio."""
    return {"result": _post("/api/stop")}


@mcp.tool
def rate_band(deck: str = "sw") -> dict:
    """Audition the deck's 40 strongest carriers for 2.5 s each and
    stamp 0-100 audio-quality stars into the grid (~2 min)."""
    return {"result": _post("/api/band/rate", {"deck": deck})}


@mcp.tool
def autotune(deck: str = "sw") -> dict:
    """One call to the best-sounding station: rates the band if
    unrated, then walks the quality ranking with live verification,
    falling back if a station died since rating."""
    return {"result": _post("/api/band/autotune", {"deck": deck})}


@mcp.tool
def sw_schedule(khz: float) -> dict:
    """Full-day EiBi broadcast schedule for one shortwave frequency:
    who transmits when (UTC), in what language, aimed where, from
    which transmitter site."""
    return _get(f"/api/sw/sched?khz={khz:g}")


@mcp.tool
def dx_logbook() -> dict:
    """Band-openings-by-hour curves and best-ever catches from the
    user's accumulated scan history."""
    return _get("/api/dx/summary")


if __name__ == "__main__":
    mcp.run()
