"""prop_history.py - the Propagation Observatory's history deck.
http://localhost:8649

Task #33 phase 3: the day/week/month/year UI over what prop_atlas.py has
already banked. This is a FILE-READER, full stop:

  * reads lab/prop_atlas.db strictly read-only (file: URI, mode=ro) -
    the atlas daemon keeps writing under WAL while we read;
  * reads lab/storm_watch_log.txt for the sferics strip + storm markers;
  * touches NO radio, imports NO SoapySDR, takes NO radio_lock - it can
    run while every SDR in the house is booked.

The house privacy law applies: the observer location comes only from
RT_QTH='lat,lon' in the environment or gitignored lab/qth.txt (am_db.py
convention). It is used ONLY to compute day/night shading server-side;
coordinates never appear in this file, in commits, or in the API.
Without a QTH the panel still works - night shading falls back to a
fixed local-clock approximation and says so.

  python tools/prop_history.py            # serve on 127.0.0.1:8649
  python tools/prop_history.py --port N   # elsewhere if 8649 is taken
"""
import argparse
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parent / "lab"
DB = LAB / "prop_atlas.db"
STORM_LOG = LAB / "storm_watch_log.txt"

PORT = 8649
BANDS = ["MW", "49m", "41m", "31m", "25m", "19m"]   # atlas order, LF->HF
BAND_RANGE = {"MW": "530-1700 kHz", "49m": "5900-6200", "41m": "7200-7450",
              "31m": "9400-9900", "25m": "11600-12100", "19m": "15100-15800"}
ALIVE_Q = 18                     # whole_band's "listenable" line (atlas law)

# span -> (seconds, bucket seconds; 0 = raw sweeps)
SPANS = {"day":   (86400,          0),
         "week":  (7 * 86400,      0),
         "month": (30 * 86400,     3 * 3600),
         "year":  (365 * 86400,    86400)}

_CACHE = {}                      # span -> (built_at, payload) - be gentle
CACHE_S = 60


# ------------------------------------------------------------------ time
def _iso_to_epoch(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _epoch_to_iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------- qth
def _qth():
    """Private observer location, house convention (am_db.py): RT_QTH
    env var or gitignored lab/qth.txt. Never from code, never served."""
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


def _sun_alt_deg(lat, lon, t):
    """Solar altitude (deg) at unix time t - low-precision NOAA-style,
    plenty for drawing a terminator."""
    n = (t - 946728000.0) / 86400.0          # days since J2000.0
    L = (280.460 + 0.9856474 * n) % 360.0
    g = math.radians((357.528 + 0.9856003 * n) % 360.0)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 4e-7 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam),
                                 math.cos(lam))) % 360.0
    gmst = (18.697374558 + 24.06570982441908 * n) % 24.0
    ha = math.radians((gmst * 15.0 + lon - ra) % 360.0)
    la = math.radians(lat)
    return math.degrees(math.asin(
        math.sin(la) * math.sin(dec) +
        math.cos(la) * math.cos(dec) * math.cos(ha)))


def night_intervals(t0, t1):
    """[[start, end], ...] of sun-below-horizon inside the window, plus
    how we knew. With no QTH: fixed local 19:00-06:30 approximation."""
    qth = _qth()
    step = max(300, int((t1 - t0) / 2400))       # <= ~2400 samples
    out, start = [], None
    if qth:
        lat, lon = qth
        t = t0
        while t <= t1:
            dark = _sun_alt_deg(lat, lon, t) < 0.0
            if dark and start is None:
                start = t
            elif not dark and start is not None:
                out.append([start, t])
                start = None
            t += step
        if start is not None:
            out.append([start, t1])
        return out, "sun"
    # no QTH: local wall-clock approximation, honestly labeled
    t = t0
    while t <= t1:
        lt = time.localtime(t)
        mins = lt.tm_hour * 60 + lt.tm_min
        dark = mins >= 19 * 60 or mins < 6 * 60 + 30
        if dark and start is None:
            start = t
        elif not dark and start is not None:
            out.append([start, t])
            start = None
        t += step
    if start is not None:
        out.append([start, t1])
    return out, "approx"


# ---------------------------------------------------------------- atlas
def _bucket(rows, bucket_s):
    """rows [(epoch, alive, nchan, strength)] -> averaged buckets."""
    if not bucket_s:
        return [[int(t), a, nc, st] for t, a, nc, st in rows]
    acc = {}
    for t, a, nc, st in rows:
        k = int(t // bucket_s) * bucket_s
        acc.setdefault(k, []).append((a, nc, st))
    out = []
    for k in sorted(acc):
        v = acc[k]
        sts = [x[2] for x in v if x[2] is not None]
        out.append([k + bucket_s // 2,
                    round(sum(x[0] for x in v) / len(v), 1),
                    max(x[1] for x in v),
                    round(sum(sts) / len(sts), 1) if sts else None])
    return out


def atlas_history(t0, t1, bucket_s):
    """Per-band [(t, alive, n_channels, mean alive-channel q)] inside
    the window, plus the window's top catches and db-wide coverage.
    Read-only and defensive - a locked/missing db returns ok=False."""
    out = {"ok": False, "bands": {}, "top": []}
    if not DB.exists():
        out["err"] = "no atlas db yet - is prop_atlas running?"
        return out
    iso0 = _epoch_to_iso(t0)
    try:
        cx = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True,
                             timeout=2.0)
    except sqlite3.Error as e:
        out["err"] = str(e)
        return out
    try:
        first, last, n_scans, n_sweeps = cx.execute(
            "SELECT (SELECT MIN(ts_utc) FROM sweeps),"
            "       (SELECT MAX(ts_utc) FROM sweeps),"
            "       (SELECT COUNT(*) FROM scans),"
            "       (SELECT COUNT(*) FROM sweeps)").fetchone()
        # strength = mean q of ALIVE channels per sweep+band (the scans
        # table is the heavy one; a single grouped pass is fine at 60 s
        # cache cadence)
        strength = {}
        for ts, band, mq in cx.execute(
                "SELECT ts_utc, band, AVG(q) FROM scans "
                "WHERE ts_utc >= ? AND q >= ? GROUP BY ts_utc, band",
                (iso0, ALIVE_Q)):
            strength[(ts, band)] = round(mq, 1)
        per = {b: [] for b in BANDS}
        for ts, band, nc, na in cx.execute(
                "SELECT ts_utc, band, n_channels, n_alive FROM sweeps "
                "WHERE ts_utc >= ? ORDER BY ts_utc", (iso0,)):
            t = _iso_to_epoch(ts)
            if t is None or t > t1 or band not in per:
                continue
            per[band].append((t, na, nc, strength.get((ts, band))))
        out["bands"] = {b: _bucket(v, bucket_s) for b, v in per.items()}
        out["top"] = [
            {"khz": k, "band": b, "q": q, "grade": g or "",
             "expected": e or ""}
            for k, b, q, g, e in cx.execute(
                "SELECT khz, band, MAX(q), grade, expected FROM scans "
                "WHERE ts_utc >= ? AND q IS NOT NULL "
                "GROUP BY khz ORDER BY 3 DESC LIMIT 15", (iso0,))]
        out.update(ok=True, first_sweep=first, last_sweep=last,
                   scan_rows=n_scans, sweep_rows=n_sweeps)
    except sqlite3.Error as e:
        out["err"] = str(e)
    finally:
        cx.close()
    return out


# ---------------------------------------------------------------- storm
_SNIFF = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) sniff: ([\d.]+) "
    r"impulses/min \((.*)\) baseline ([\d.]+) -> (\w+)")
_TWEEK = re.compile(r"(\d+) good tweeks")


def storm_history(t0, t1):
    """storm_watch sniffs in-window: [[t, impulses/min, state, tweeks]].
    Anything not QUIET/LEARNING is a storm marker."""
    rows = []
    try:
        lines = STORM_LOG.read_text(encoding="utf-8",
                                    errors="replace").splitlines()
    except OSError:
        return rows
    for ln in lines:
        m = _SNIFF.match(ln)
        if not m:
            continue
        t = _iso_to_epoch(m.group(1))
        if t is None or not (t0 <= t <= t1):
            continue
        tw = _TWEEK.search(m.group(3))
        rows.append([int(t), float(m.group(2)), m.group(5),
                     int(tw.group(1)) if tw else 0])
    return rows


def build_payload(span):
    now = time.time()
    hit = _CACHE.get(span)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    secs, bucket_s = SPANS[span]
    t0, t1 = now - secs, now
    atlas = atlas_history(t0, t1, bucket_s)
    night, night_src = night_intervals(t0, t1)
    payload = {"span": span, "t0": int(t0), "t1": int(t1),
               "bucket_s": bucket_s, "alive_q": ALIVE_Q,
               "band_order": BANDS, "band_range": BAND_RANGE,
               "atlas": atlas,
               "storm": storm_history(t0, t1),
               "night": night, "night_src": night_src,
               "generated": _epoch_to_iso(now)}
    _CACHE[span] = (now, payload)
    return payload


# ------------------------------------------------------------- the page
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>PROP HISTORY</title><style>
body{font-family:Consolas,'Lucida Console',monospace;color:#9fd4e0;
margin:0;padding:18px;min-height:100vh;background:#030509;
background-image:linear-gradient(rgba(0,229,255,.05) 1px,transparent 1px),
linear-gradient(90deg,rgba(0,229,255,.05) 1px,transparent 1px);
background-size:44px 44px}
body::after{content:'';position:fixed;inset:0;pointer-events:none;
background:repeating-linear-gradient(0deg,rgba(0,0,0,.14) 0 1px,
transparent 1px 3px)}
h1{font-size:24px;margin:0;letter-spacing:4px;color:#00e5ff;
text-shadow:0 0 14px rgba(0,229,255,.8),0 0 40px rgba(0,229,255,.35)}
h1 .mag{color:#ff2bd6;text-shadow:0 0 14px rgba(255,43,214,.8)}
.sub{color:#3f6a78;font-size:11px;margin-bottom:14px;letter-spacing:2px}
#cabinet{max-width:1080px;margin:0 auto;background:rgba(6,11,20,.92);
border:1px solid rgba(0,229,255,.35);border-radius:8px;
box-shadow:0 0 24px rgba(0,229,255,.12),inset 0 0 60px rgba(0,0,0,.5);
padding:20px}
button{cursor:pointer;font-family:Consolas,monospace}
.knob{background:#04070f;color:#9fd4e0;border:1px solid #00e5ff;
border-radius:4px;padding:7px 18px;font-size:13px;letter-spacing:1px}
.knob:hover{box-shadow:0 0 14px rgba(0,229,255,.6);color:#fff}
.knob.on{border-color:#ff2bd6;color:#ff8fe8;
box-shadow:0 0 14px rgba(255,43,214,.5)}
#zoom{text-align:center;margin:8px 0 14px}
#coverage{text-align:center;color:#7ab8c8;font-size:12px;min-height:18px}
#wrap{position:relative;margin-top:10px}
#chart{width:100%;display:block;background:#02040a;
border:1px solid rgba(0,229,255,.3);border-radius:6px}
#tip{position:absolute;display:none;pointer-events:none;z-index:5;
background:rgba(4,8,16,.95);border:1px solid rgba(0,229,255,.5);
border-radius:5px;padding:7px 10px;font-size:12px;color:#c8ecf4;
box-shadow:0 0 12px rgba(0,229,255,.25);white-space:nowrap}
#tip .h{color:#00e5ff;margin-bottom:3px}
#tip .sw{display:inline-block;width:8px;height:8px;border-radius:2px;
margin-right:5px;vertical-align:middle}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
td,th{padding:6px 8px;border-bottom:1px solid rgba(0,229,255,.12);
text-align:left}
th{color:#3f6a78;font-size:10px;letter-spacing:2px}
tr:hover td{background:rgba(0,229,255,.04)}
details{margin-top:16px;border:1px solid rgba(255,43,214,.35);
border-radius:6px;background:#04070f}
details summary{cursor:pointer;padding:8px 12px;color:#ff2bd6;
letter-spacing:3px;font-size:12px;
text-shadow:0 0 10px rgba(255,43,214,.5)}
details .inner{padding:0 12px 12px;max-height:340px;overflow:auto}
.note{color:#3f6a78;font-size:11px;margin-top:8px}
</style></head><body>
<div id="cabinet">
<h1>PROP HISTORY <span class="mag">OBSERVATORY</span></h1>
<div class="sub">TASK #33 &middot; 24/7 AM+SW ATLAS &middot; FILE-READER
 OVER lab/prop_atlas.db &middot; NO RADIO TOUCHED</div>
<div id="zoom">
<button class="knob" data-span="day">DAY</button>
<button class="knob" data-span="week">WEEK</button>
<button class="knob" data-span="month">MONTH</button>
<button class="knob" data-span="year">YEAR</button>
</div>
<div id="coverage">loading&hellip;</div>
<div id="wrap"><canvas id="chart"></canvas><div id="tip"></div></div>
<div class="note" id="legendnote"></div>
<details><summary>TOP CATCHES THIS WINDOW</summary>
<div class="inner"><table id="toptab"></table></div></details>
<details><summary>DATA TABLE (SWEEPS IN WINDOW)</summary>
<div class="inner"><table id="datatab"></table></div></details>
</div>
<script>
"use strict";
/* categorical band palette - validated (CVD deltaE>=14, contrast>=3:1 on
   #030509); the neon lightness is the house CRT look, and every strip is
   direct-labeled so identity never rides on color alone */
const COL={MW:"#ffb457","49m":"#00e5ff","41m":"#ff2bd6","31m":"#39ff8a",
           "25m":"#9f8cff","19m":"#ffe14a"};
const STORM_COL="#ff2bd6", INK="#9fd4e0", MUT="#3f6a78";
let span="day", data=null, geom=null;
const cv=document.getElementById("chart"), cx=cv.getContext("2d");
const tip=document.getElementById("tip");

function fmtT(t,sp){
  const d=new Date(t*1000);
  const hh=String(d.getHours()).padStart(2,"0"),
        mm=String(d.getMinutes()).padStart(2,"0");
  if(sp==="day") return hh+":"+mm;
  const mo=String(d.getMonth()+1).padStart(2,"0"),
        dd=String(d.getDate()).padStart(2,"0");
  if(sp==="week"||sp==="month") return mo+"/"+dd+" "+hh+":"+mm;
  return d.getFullYear()+"-"+mo+"-"+dd;
}
function axisTicks(t0,t1,sp){
  const out=[], d=new Date(t0*1000);
  let step, al;
  if(sp==="day"){step=3*3600; d.setMinutes(0,0,0);
    d.setHours(d.getHours()-d.getHours()%3);}
  else if(sp==="week"){step=86400; d.setHours(0,0,0,0);}
  else if(sp==="month"){step=5*86400; d.setHours(0,0,0,0);}
  else{step=30*86400; d.setHours(0,0,0,0);}
  for(let t=d.getTime()/1000;t<=t1;t+=step) if(t>=t0) out.push(t);
  return out;
}
function labelFor(t,sp){
  const d=new Date(t*1000);
  if(sp==="day") return String(d.getHours()).padStart(2,"0")+"h";
  const mo=String(d.getMonth()+1).padStart(2,"0"),
        dd=String(d.getDate()).padStart(2,"0");
  return mo+"/"+dd;
}

function draw(){
  if(!data) return;
  const dpr=window.devicePixelRatio||1;
  const W=cv.clientWidth||cv.parentElement.clientWidth;
  const bands=data.band_order, NB=bands.length;
  const stripH=64, stormH=76, padT=8, padB=26, padL=64, padR=14, gap=10;
  const H=padT+NB*(stripH+gap)+stormH+padB;
  cv.style.height=H+"px";
  cv.width=W*dpr; cv.height=H*dpr; cx.setTransform(dpr,0,0,dpr,0,0);
  cx.clearRect(0,0,W,H);
  const t0=data.t0,t1=data.t1,X=t=>padL+(t-t0)/(t1-t0)*(W-padL-padR);
  const plotW=W-padL-padR;
  geom={X,t0,t1,padL,padT,stripH,gap,stormH,W,H,plotW,padB};

  /* night shading behind everything */
  const plotH=NB*(stripH+gap)+stormH;
  for(const [a,b] of data.night){
    const x0=Math.max(padL,X(a)), x1=Math.min(W-padR,X(b));
    if(x1-x0<0.5) continue;
    cx.fillStyle="rgba(0,229,255,0.045)";
    cx.fillRect(x0,padT,x1-x0,plotH);
  }
  /* time grid + labels */
  cx.font="10px Consolas,monospace"; cx.textAlign="center";
  for(const t of axisTicks(t0,t1,span)){
    const x=X(t);
    cx.strokeStyle="rgba(0,229,255,0.10)"; cx.beginPath();
    cx.moveTo(x,padT); cx.lineTo(x,padT+plotH); cx.stroke();
    cx.fillStyle=MUT; cx.fillText(labelFor(t,span),x,H-10);
  }
  /* band strips: n_alive as area+line, direct-labeled */
  bands.forEach((b,i)=>{
    const y0=padT+i*(stripH+gap), rows=data.atlas.bands[b]||[];
    const nch=rows.length?Math.max(...rows.map(r=>r[2])):1;
    const ymax=Math.max(5,...rows.map(r=>r[1]));
    const Y=v=>y0+stripH-4-(v/ymax)*(stripH-16);
    cx.strokeStyle="rgba(0,229,255,0.15)";
    cx.strokeRect(padL,y0,plotW,stripH);
    if(rows.length){
      /* honesty: a sweep drought is a GAP, not a flat line - break
         segments when consecutive points sit > 3 cadences apart */
      const gapMax=3*Math.max(data.bucket_s||0,1800);
      for(const seg of segments(rows,gapMax)){
        if(seg.length===1){const x=X(seg[0][0]),y=Y(seg[0][1]);
          cx.fillStyle=COL[b];cx.beginPath();cx.arc(x,y,3,0,7);cx.fill();
          continue;}
        cx.beginPath();
        seg.forEach((r,j)=>{const x=X(r[0]),y=Y(r[1]);
          j?cx.lineTo(x,y):cx.moveTo(x,y);});
        cx.strokeStyle=COL[b]; cx.lineWidth=2; cx.stroke();
        cx.lineTo(X(seg[seg.length-1][0]),y0+stripH-4);
        cx.lineTo(X(seg[0][0]),y0+stripH-4); cx.closePath();
        cx.fillStyle=COL[b]+"22"; cx.fill(); cx.lineWidth=1;
      }
    }else{
      cx.fillStyle=MUT; cx.textAlign="center";
      cx.fillText("no sweeps in window",padL+plotW/2,y0+stripH/2);
    }
    cx.textAlign="left"; cx.font="12px Consolas,monospace";
    cx.fillStyle=COL[b]; cx.fillText(b,8,y0+14);
    cx.font="9px Consolas,monospace"; cx.fillStyle=MUT;
    cx.fillText(data.band_range[b],8,y0+26);
    cx.fillText("alive",8,y0+40);
    cx.fillText("max "+(rows.length?Math.max(...rows.map(r=>r[1])):0)+
                "/"+nch,8,y0+50);
    cx.font="10px Consolas,monospace";
  });
  /* sferics strip */
  const sy=padT+NB*(stripH+gap), st=data.storm;
  cx.strokeStyle="rgba(0,229,255,0.15)"; cx.strokeRect(padL,sy,plotW,stormH);
  cx.textAlign="left"; cx.font="12px Consolas,monospace";
  cx.fillStyle=INK; cx.fillText("SFERICS",8,sy+14);
  cx.font="9px Consolas,monospace"; cx.fillStyle=MUT;
  cx.fillText("impulses",8,sy+26); cx.fillText("per min",8,sy+36);
  if(st.length){
    const smax=Math.max(...st.map(r=>r[1]));
    const Ys=v=>sy+stormH-4-(v/smax)*(stormH-18);
    for(const seg of segments(st,3*3600*3)){
      if(seg.length===1){const x=X(seg[0][0]),y=Ys(seg[0][1]);
        cx.fillStyle=INK;cx.beginPath();cx.arc(x,y,3,0,7);cx.fill();
        continue;}
      cx.beginPath();
      seg.forEach((r,j)=>{const x=X(r[0]),y=Ys(r[1]);
        j?cx.lineTo(x,y):cx.moveTo(x,y);});
      cx.strokeStyle=INK; cx.lineWidth=1.5; cx.stroke();
    }
    cx.lineWidth=1;
    for(const r of st) if(r[2]!=="QUIET"&&r[2]!=="LEARNING"){
      const x=X(r[0]), y=Ys(r[1]);
      cx.fillStyle=STORM_COL; cx.beginPath();
      cx.moveTo(x,y-12); cx.lineTo(x-5,y-3); cx.lineTo(x+5,y-3);
      cx.closePath(); cx.fill();
      cx.font="9px Consolas,monospace";
      cx.fillText(r[2],Math.min(x+7,W-70),y-6);
    }
    cx.fillStyle=MUT; cx.font="9px Consolas,monospace";
    cx.fillText("max "+Math.round(smax),8,sy+50);
  }else{
    cx.fillStyle=MUT; cx.textAlign="center";
    cx.fillText("no storm_watch sniffs in window",padL+plotW/2,
                sy+stormH/2);
    cx.textAlign="left";
  }
}

function segments(rows,gapMax){
  const out=[]; let cur=[];
  for(const r of rows){
    if(cur.length&&r[0]-cur[cur.length-1][0]>gapMax){out.push(cur);cur=[];}
    cur.push(r);
  }
  if(cur.length) out.push(cur);
  return out;
}
function nearest(rows,t){
  if(!rows||!rows.length) return null;
  let best=rows[0];
  for(const r of rows) if(Math.abs(r[0]-t)<Math.abs(best[0]-t)) best=r;
  return best;
}
cv.addEventListener("mousemove",e=>{
  if(!data||!geom) return;
  const r=cv.getBoundingClientRect(), x=e.clientX-r.left;
  if(x<geom.padL||x>geom.W-14){tip.style.display="none";draw();return;}
  const t=geom.t0+(x-geom.padL)/geom.plotW*(geom.t1-geom.t0);
  draw();
  cx.strokeStyle="rgba(255,255,255,0.25)"; cx.beginPath();
  cx.moveTo(x,geom.padT); cx.lineTo(x,geom.H-geom.padB); cx.stroke();
  let html='<div class="h">'+fmtT(t,span)+" local</div>";
  for(const b of data.band_order){
    const n=nearest(data.atlas.bands[b],t);
    if(!n||Math.abs(n[0]-t)>(data.bucket_s||1800)*2) continue;
    html+='<div><span class="sw" style="background:'+COL[b]+'"></span>'+
      b+": "+n[1]+"/"+n[2]+" alive"+
      (n[3]!=null?" &middot; Q&#772;"+n[3]:"")+"</div>";
  }
  const s=nearest(data.storm,t);
  if(s&&Math.abs(s[0]-t)<=5400)
    html+="<div>sferics: "+Math.round(s[1])+"/min &middot; "+s[2]+
          (s[3]?" &middot; "+s[3]+" tweeks":"")+"</div>";
  tip.innerHTML=html; tip.style.display="block";
  const wr=document.getElementById("wrap").getBoundingClientRect();
  tip.style.left=Math.min(x+14,wr.width-tip.offsetWidth-4)+"px";
  tip.style.top=(e.clientY-wr.top+12)+"px";
});
cv.addEventListener("mouseleave",()=>{tip.style.display="none";draw();});

function esc(s){return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
  .replace(/>/g,"&gt;");}
function tables(){
  const tt=document.getElementById("toptab");
  let h="<tr><th>KHZ</th><th>BAND</th><th>Q</th><th>GRADE</th>"+
        "<th>SCHEDULED (EiBi)</th></tr>";
  for(const r of data.atlas.top||[])
    h+="<tr><td>"+r.khz+"</td><td>"+r.band+"</td><td>"+r.q+"</td><td>"+
       esc(r.grade)+"</td><td>"+esc(r.expected)+"</td></tr>";
  tt.innerHTML=h;
  const dt=document.getElementById("datatab");
  let g="<tr><th>TIME (LOCAL)</th>"+
        data.band_order.map(b=>"<th>"+b+"</th>").join("")+"</tr>";
  const times=new Set();
  for(const b of data.band_order)
    for(const r of data.atlas.bands[b]||[]) times.add(r[0]);
  const ts=[...times].sort((a,b)=>b-a).slice(0,200);
  for(const t of ts){
    g+="<tr><td>"+fmtT(t,"week")+"</td>";
    for(const b of data.band_order){
      const n=nearest(data.atlas.bands[b],t);
      g+="<td>"+(n&&Math.abs(n[0]-t)<60?n[1]+"/"+n[2]:"&middot;")+"</td>";
    }
    g+="</tr>";
  }
  dt.innerHTML=g;
}

async function load(){
  const r=await fetch("/api/history?span="+span);
  data=await r.json();
  const a=data.atlas, cov=document.getElementById("coverage");
  if(!a.ok){cov.textContent="atlas: "+(a.err||"unavailable");return;}
  const days=((Date.parse(a.last_sweep)-Date.parse(a.first_sweep))
              /86400000).toFixed(1);
  cov.innerHTML="atlas first light "+a.first_sweep.slice(0,10)+
    " &middot; "+days+" days &middot; "+a.sweep_rows+" sweeps &middot; "+
    a.scan_rows.toLocaleString()+" channel scans &middot; last "+
    a.last_sweep+((span==="month"||span==="year")?
    " &middot; <span style='color:#ffb457'>window wider than the data"+
    " &mdash; the atlas is "+days+" days old</span>":"");
  document.getElementById("legendnote").innerHTML=
    "shaded columns = "+(data.night_src==="sun"?
      "sun below horizon (RT_QTH)":"approx local night (no QTH set)")+
    " &middot; \\u25b2 = storm_watch state above QUIET &middot; "+
    "alive = channel Q &ge; "+data.alive_q+" &middot; "+
    "Q&#772; = mean quality of alive channels &middot; refreshes 5 min";
  draw(); tables();
}
document.querySelectorAll("#zoom .knob").forEach(b=>{
  b.addEventListener("click",()=>{
    span=b.dataset.span;
    document.querySelectorAll("#zoom .knob").forEach(x=>
      x.classList.toggle("on",x===b));
    load();
  });
});
document.querySelector('[data-span="day"]').classList.add("on");
window.addEventListener("resize",draw);
load(); setInterval(load,300000);
</script></body></html>"""


# ---------------------------------------------------------------- serve
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(PAGE, "text/html; charset=utf-8")
        elif self.path.startswith("/api/history"):
            span = "day"
            if "span=" in self.path:
                cand = self.path.split("span=")[1].split("&")[0]
                if cand in SPANS:
                    span = cand
            try:
                self._send(json.dumps(build_payload(span)))
            except Exception as e:
                self._send(json.dumps({"ok": False, "err": str(e)}))
        else:
            self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(
        (os.environ.get("PROP_HISTORY_BIND", "127.0.0.1"), args.port), H)
    print(f"[prop_history] file-reader up on "
          f"http://127.0.0.1:{args.port} (db: {DB})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
