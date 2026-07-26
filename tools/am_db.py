"""am_db.py - Radio Tuna: per-user AM station database, scraped fresh.

No station list ships with this code. On first AM scan the panel pulls
the FCC's public AM Query (every licensed station 530-1700 kHz: callsign,
city, power, hours, tower coordinates) into lab/am_db.json - YOUR copy,
on YOUR disk, filled by YOUR scans. lab/ is gitignored by design.

Ranking is physics, not vibes:
  - DAY-licensed stations are off the air at night - excluded after
    local sunset (approximated by the clock hour)
  - with RT_QTH="lat,lon" set in the environment (kept private - never
    put coordinates in code or commits), candidates rank by a groundwave
    score sqrt(kW)/distance, and far Class-A 50 kW stations at night get
    the honest "(skywave)" tag
  - without RT_QTH, power is the ranking

  python am_db.py fetch          # force-refresh the cache
  python am_db.py lookup 820     # who is on 820?
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parent / "lab"
LAB.mkdir(exist_ok=True)
CACHE = LAB / "am_db.json"
MAX_AGE_S = 30 * 86400

AMQ = ("https://transition.fcc.gov/fcc-bin/amq?state=&call=&city="
       "&freq=530&fre2=1700&type=0&facid=&class=&list=4&dist=&dlat2=&dlon2=&size=9")

# station facts that are national knowledge, not location data
NOTES = {"WSHE": "ALL-DIGITAL MA3"}


def _parse_dms(sign, d, m, s):
    try:
        v = float(d) + float(m) / 60.0 + float(s) / 3600.0
    except ValueError:
        return None
    return -v if sign in ("W", "S") else v


def fetch(force=False):
    """Pull the whole AM band from the FCC AM Query into lab/am_db.json."""
    if CACHE.exists() and not force and time.time() - CACHE.stat().st_mtime < MAX_AGE_S:
        return CACHE
    # the FCC front door 403s unknown agents; identify as a plain client
    req = urllib.request.Request(AMQ, headers={"User-Agent": "curl/8.4.0"})
    text = urllib.request.urlopen(req, timeout=120).read().decode("latin-1")
    recs = []
    for line in text.splitlines():
        p = [c.strip() for c in line.split("|")]
        if len(p) < 28 or not p[1] or "kHz" not in p[2]:
            continue
        if p[9] != "LIC":                      # licensed stations only
            continue
        try:
            khz = int(p[2].split()[0])
            kw = float(p[14].split()[0]) if p[14] else 0.0
        except (ValueError, IndexError):
            continue
        recs.append({
            "call": p[1], "khz": khz, "hours": p[5], "cls": p[7] or p[8],
            "city": p[10].title(), "st": p[11], "cc": p[12], "kw": kw,
            "lat": _parse_dms(p[19], p[20], p[21], p[22]),
            "lon": _parse_dms(p[23], p[24], p[25], p[26]),
        })
    if len(recs) < 1000:
        raise RuntimeError(f"AM Query returned only {len(recs)} stations - "
                           "format change or truncated fetch; cache not written")
    CACHE.write_text(json.dumps(recs))
    print(f"[am_db] fetched {len(recs)} licensed AM stations -> {CACHE}")
    return CACHE


def _load():
    if not CACHE.exists() or time.time() - CACHE.stat().st_mtime > MAX_AGE_S:
        fetch()
    return json.loads(CACHE.read_text())


def _qth():
    """Private observer location: RT_QTH='lat,lon' env var, or a
    lab/qth.txt with the same format. lab/ is gitignored - coordinates
    never live in code or commits."""
    v = os.environ.get("RT_QTH", "")
    if not v:
        try:
            v = (LAB / "qth.txt").read_text().strip()
        except OSError:
            return None
    try:
        lat, lon = (float(t) for t in v.split(","))
        return lat, lon
    except ValueError:
        return None


def _dist_km(a_lat, a_lon, b_lat, b_lon):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _is_night(hour=None):
    h = time.localtime().tm_hour if hour is None else hour
    return h >= 19 or h < 7


def lookup(khz, hour=None):
    """Best-guess identification for a carrier on `khz`.
    Returns (label, call, link, n_on_channel); empty strings if unknown."""
    try:
        db = _load()
    except Exception:
        return "", "", "", 0
    night = _is_night(hour)
    cands = [r for r in db if r["khz"] == int(round(khz))]
    if night:
        cands = [r for r in cands if r["hours"] != "DAY"]   # daytimers sign off
    if not cands:
        return "", "", "", 0
    qth = _qth()
    if qth:
        for r in cands:
            if r["lat"] is not None and r["lon"] is not None:
                r["_km"] = _dist_km(qth[0], qth[1], r["lat"], r["lon"])
            else:
                r["_km"] = 9e9
        cands.sort(key=lambda r: -math.sqrt(max(r["kw"], .01)) / max(r["_km"], 1.0))
    else:
        cands.sort(key=lambda r: -r["kw"])
    top = cands[0]
    label = f"{top['call']} {top['city']} {top['st']} · {top['kw']:g} kW"
    note = NOTES.get(top["call"])
    if note:
        label += f" — {note}"
    if qth and top.get("_km", 0) > 400 and night:
        label += " (skywave)"
    if len(cands) > 1:
        label += f"  [+{len(cands)-1} on ch]"
    link = f"https://radio-locator.com/info/{top['call']}-AM"
    return label, top["call"], link, len(cands)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    lk = sub.add_parser("lookup")
    lk.add_argument("khz", type=float)
    args = ap.parse_args()
    if args.cmd == "fetch":
        fetch(force=True)
    else:
        label, call, link, n = lookup(args.khz)
        print(label or "(no licensed match)")
        if link:
            print(link)


if __name__ == "__main__":
    main()
