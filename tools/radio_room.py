"""radio_room.py - Radio Tuna's listening room for the whole dial.  :8645

The Broadcast Guide, clickable: every station the survey found - FM, AM,
shortwave (with EiBi names) - each with a LISTEN button. A click captures
~25 s, demodulates with the quality chain (channel filter -> shaping ->
fade-riding AGC), grades the audio honestly, and plays it in the browser.

Plays nice with the observatory: if the SDR is reserved (balloon window,
satellite pass, warden rotation) the room says so instead of fighting.

  python radio_room.py        ->  http://localhost:8645
"""
import json
import threading
import time
import sys
import wave
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hf_knob import open_sdr, grab, FS               # noqa: E402
from broadcast_guide import load_eibi, on_air_now, fetch_eibi   # noqa: E402
from sw_listen import am_demod_wav                   # noqa: E402

LAB = HERE.parent / "lab"
DATA = LAB / "guide_data.json"
STATIONS = LAB / "stations.json"
WAV = LAB / "room_last.wav"
PORT = 8645

STATE = {"phase": "idle", "msg": "", "last": None}
LOCK = threading.Lock()


def fm_demod_wav(iq, out_path, fs=FS, aud=48_000):
    """Mono WBFM with deemphasis + the same shaping/AGC philosophy."""
    from scipy.signal import resample_poly, butter, sosfilt
    from math import gcd
    disc = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)
    g = gcd(int(aud), int(fs))
    audio = resample_poly(disc, int(aud) // g, int(fs) // g).astype(np.float32)
    # 75 us deemphasis (single pole)
    a = float(np.exp(-1.0 / (aud * 75e-6)))
    audio = sosfilt([[1 - a, 0, 0, 1, -a, 0]], audio).astype(np.float32)
    sos = butter(4, [30, 15000], btype="band", fs=aud, output="sos")
    audio = sosfilt(sos, audio).astype(np.float32)
    k = aud
    p = np.convolve(audio ** 2, np.ones(k, np.float32) / k, mode="same")
    gain = np.clip(0.25 / (np.sqrt(p) + 1e-4), 0, 60.0)
    audio = np.clip(audio * gain, -0.95, 0.95)
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(aud)
        w.writeframes(pcm.tobytes())
    return len(pcm) / aud


def grade_audio(wav_path):
    """Honest quality dial: speech-band energy vs hiss-band energy."""
    with wave.open(str(wav_path), "rb") as w:
        fr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
    if len(x) < fr:
        return {"snr_db": 0, "grade": "?"}
    spec = np.abs(np.fft.rfft(x[: fr * 20] * np.hanning(min(len(x), fr * 20)))) ** 2
    freqs = np.fft.rfftfreq(min(len(x), fr * 20), 1 / fr)
    sp = spec[(freqs > 300) & (freqs < 3000)].mean()
    hiss = spec[(freqs > 5000) & (freqs < 7000)].mean() + 1e-9
    snr = 10 * np.log10(sp / hiss)
    grade = ("EXCELLENT" if snr > 25 else "GOOD" if snr > 15
             else "FAIR" if snr > 8 else "POOR")
    return {"snr_db": round(float(snr), 1), "grade": grade}


def do_listen(band, freq_khz, secs=25):
    with LOCK:
        if STATE["phase"] not in ("idle", "ready"):
            return
        STATE.update({"phase": "capturing",
                      "msg": f"tuning {freq_khz:g} kHz ({band})"})
    try:
        sdr, st = open_sdr("Antenna C" if band != "fm" else "Antenna A")
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX
        sdr.setFrequency(SOAPY_SDR_RX, 0, freq_khz * 1e3)
        time.sleep(0.2)
        iq = grab(sdr, st, secs)
        sdr.deactivateStream(st)
        sdr.closeStream(st)
        STATE.update({"phase": "demod", "msg": "demodulating"})
        if band == "fm":
            fm_demod_wav(iq, WAV)
        else:
            am_demod_wav(iq, WAV)
        q = grade_audio(WAV)
        STATE.update({"phase": "ready",
                      "msg": f"{freq_khz:g} kHz - audio SNR {q['snr_db']} dB ({q['grade']})",
                      "last": {"khz": freq_khz, "band": band, **q,
                               "ts": time.time()}})
    except Exception as e:
        busy = "no available RSP" in str(e) or "Device_make" in str(e)
        STATE.update({"phase": "idle",
                      "msg": ("radio reserved (balloon/satellite window or "
                              "rotation) - try again in a minute")
                      if busy else f"error: {e}"})


def build_rows():
    if not DATA.exists():
        return []
    d = json.loads(DATA.read_text())
    eibi = load_eibi()
    hd = {}
    if STATIONS.exists():
        try:
            for s in json.loads(STATIONS.read_text()).get("stations", []):
                hd[round(float(s.get("mhz", 0)), 1)] = s.get("name") or ""
        except Exception:
            pass
    rows = []
    for s in d.get("fm", []):
        rows.append({"band": "fm", "khz": s["mhz"] * 1000,
                     "label": f"{s['mhz']:.1f} FM", "snr": s["snr_db"],
                     "name": hd.get(s["mhz"], "") + (" [HD]" if s["hd"] else "")})
    for s in d.get("am", [])[:20]:
        rows.append({"band": "am", "khz": float(s["khz"]),
                     "label": f"{s['khz']} AM", "snr": s["snr_db"],
                     "name": "HD/IBOC" if s.get("iboc") else ""})
    for s in d.get("sw", [])[:20]:
        who = on_air_now(eibi, s["khz"])
        nm = "; ".join(f"{st} ({lg})" for st, lg, _ in who[:1]) if who else ""
        if s.get("drm"):
            nm = (nm + " DRM-digital").strip()
        rows.append({"band": "sw", "khz": float(s["khz"]),
                     "label": f"{s['khz']} kHz SW", "snr": s["snr_db"],
                     "name": nm})
    return rows


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Radio Tuna - Radio Room</title><style>
body{background:#0d1014;color:#c6d0dc;font-family:Consolas,monospace;
font-size:14px;margin:0;padding:20px}
h1{color:#eaf0f6;font-size:16px;letter-spacing:.12em}
#bar{position:sticky;top:0;background:#12161c;border:1px solid #20272f;
border-radius:8px;padding:10px 14px;margin-bottom:14px}
#msg{color:#ffb43a}
audio{width:100%;margin-top:8px}
table{width:100%;border-collapse:collapse}
td,th{padding:5px 8px;border-bottom:1px solid #1b232d;text-align:left}
th{color:#7c8794;font-size:11px;letter-spacing:.1em}
button{background:#0a0d11;color:#33d0c4;border:1px solid #20272f;
border-radius:6px;padding:3px 12px;font-family:inherit;cursor:pointer}
button:hover{border-color:#33d0c4}
.snr{color:#ffb43a}.nm{color:#eaf0f6}
.b-sw td:first-child{color:#33d0c4}.b-fm td:first-child{color:#ffb43a}
.b-am td:first-child{color:#8a7dff}
</style></head><body>
<h1>RADIO ROOM - click to listen (25 s capture, quality-graded)</h1>
<div id="bar"><span id="msg">idle</span><audio id="au" controls></audio></div>
<table><thead><tr><th>STATION</th><th>RF SNR</th><th>WHO</th><th></th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
async function load(){
  let r=await fetch('/api/rows');let rows=await r.json();
  let tb=document.getElementById('rows');tb.innerHTML='';
  rows.forEach(s=>{
    let tr=document.createElement('tr');tr.className='b-'+s.band;
    tr.innerHTML=`<td>${s.label}</td><td class="snr">+${s.snr} dB</td>
      <td class="nm">${s.name||''}</td>
      <td><button onclick="listen('${s.band}',${s.khz})">LISTEN</button></td>`;
    tb.appendChild(tr);});
}
async function listen(band,khz){
  await fetch(`/api/listen?band=${band}&khz=${khz}`);
  poll();
}
let t=null;
async function poll(){
  clearTimeout(t);
  let r=await fetch('/api/status');let s=await r.json();
  document.getElementById('msg').textContent=s.msg||s.phase;
  if(s.phase==='ready'&&s.last){
    let au=document.getElementById('au');
    au.src='/audio?ts='+s.last.ts;au.play().catch(()=>{});
  } else { t=setTimeout(poll,1500); }
}
load();poll();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/rows":
                return self._send(200, json.dumps(build_rows()))
            if u.path == "/api/status":
                return self._send(200, json.dumps(STATE))
            if u.path == "/api/listen":
                q = parse_qs(u.query)
                band = q.get("band", ["sw"])[0]
                khz = float(q.get("khz", ["15500"])[0])
                threading.Thread(target=do_listen, args=(band, khz),
                                 daemon=True).start()
                return self._send(200, json.dumps({"ok": True}))
            if u.path == "/audio":
                if WAV.exists():
                    return self._send(200, WAV.read_bytes(), "audio/wav")
                return self._send(404, json.dumps({"error": "no audio yet"}))
            return self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}))


def main():
    fetch_eibi()
    print(f"radio room -> http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
