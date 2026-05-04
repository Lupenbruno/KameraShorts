"""AsfaltTV Dashboard."""
import json
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template_string, request, send_file

CONFIG_PATH = Path("config.yaml")
CLIPS_DIR = Path("data/clips")
LOG_PATH = Path("logs/pipeline.log")
QUEUE_PATH = Path("data/queue/upload_queue.json")

app = Flask(__name__)
_log_queue: queue.Queue = queue.Queue(maxsize=500)
_pipeline_running = threading.Event()


def _tail_log(n=200) -> list[str]:
    if not LOG_PATH.exists():
        return []
    return LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


def _clips_info() -> list[dict]:
    if not CLIPS_DIR.exists():
        return []
    clips = sorted(CLIPS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for c in clips[:30]:
        stat = c.stat()
        result.append({
            "name": c.name,
            "size_kb": round(stat.st_size / 1024),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m %H:%M"),
        })
    return result


def _queue_count() -> int:
    if not QUEUE_PATH.exists():
        return 0
    try:
        return len(json.loads(QUEUE_PATH.read_text(encoding="utf-8")))
    except Exception:
        return 0


def _active_cameras() -> int:
    try:
        import requests as req
        s = req.Session()
        s.headers["User-Agent"] = "KameraShorts/1.0"
        data = s.get("https://seyret.ankara.bel.tr/status.json", timeout=8).json()
        return sum(1 for v in data if v.get("is_active") and v.get("relay_last_started_at"))
    except Exception:
        return -1


def _run_pipeline(count: int, upload: bool):
    _pipeline_running.set()
    cmd = [sys.executable, "main.py", "--now", f"--count={count}"]
    if not upload:
        cmd.append("--no-upload")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(Path(__file__).parent),
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                try:
                    _log_queue.put_nowait(line)
                except queue.Full:
                    pass
        proc.wait()
    finally:
        _pipeline_running.clear()
        try:
            _log_queue.put_nowait("__DONE__")
        except queue.Full:
            pass


TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AsfaltTV</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111;color:#ddd;font-family:'Segoe UI',system-ui,sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}

.topbar{background:#161616;border-bottom:1px solid #222;height:50px;padding:0 18px;display:flex;align-items:center;gap:16px;flex-shrink:0}
.logo{font-size:15px;font-weight:700;color:#fff;letter-spacing:.3px}
.divider{width:1px;height:18px;background:#282828}
.stat{text-align:center}
.stat .v{font-size:16px;font-weight:700;line-height:1}
.stat .v.g{color:#4caf50}
.stat .v.y{color:#ffc107}
.stat .k{font-size:9px;color:#444;margin-top:2px;text-transform:uppercase;letter-spacing:.4px}
.spacer{flex:1}
.yt-lbl{display:flex;align-items:center;gap:5px;font-size:12px;color:#666;cursor:pointer}
.yt-lbl input{accent-color:#e63946;cursor:pointer}
.cnt{width:46px;background:#1e1e1e;border:1px solid #2a2a2a;color:#ccc;padding:5px 6px;border-radius:5px;font-size:12px;text-align:center}
.run-btn{background:#e63946;color:#fff;border:none;border-radius:5px;padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer}
.run-btn:disabled{opacity:.35;cursor:not-allowed}
.run-btn:not(:disabled):hover{background:#c8303c}
.pill{font-size:11px;color:#444;display:flex;align-items:center;gap:5px}
.dot{width:6px;height:6px;border-radius:50%;background:#333;transition:background .3s}
.dot.run{background:#e63946;animation:bl 1s infinite}
.dot.ok{background:#4caf50}
@keyframes bl{0%,100%{opacity:1}50%{opacity:.15}}

.body{display:grid;grid-template-columns:1fr 280px;flex:1;min-height:0}
.log-wrap{display:flex;flex-direction:column;border-right:1px solid #1c1c1c;min-height:0}
.ph{padding:8px 14px;border-bottom:1px solid #1c1c1c;font-size:10px;font-weight:600;color:#444;text-transform:uppercase;letter-spacing:.5px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.clr{background:none;border:1px solid #222;color:#444;border-radius:3px;padding:1px 7px;font-size:9px;cursor:pointer}
.clr:hover{color:#888;border-color:#333}
.log{flex:1;overflow-y:auto;padding:8px 14px;font-family:Consolas,'Cascadia Code',monospace;font-size:11px;line-height:1.75;color:#555}
.log .i{color:#3d6e8a}
.log .ok{color:#3d7a42}
.log .w{color:#8a6830}
.log .e{color:#8a3030}
.log .m{color:#777}

.clips-wrap{display:flex;flex-direction:column;min-height:0}
.clist{flex:1;overflow-y:auto}
.crow{padding:8px 12px;border-bottom:1px solid #191919;display:flex;align-items:center;gap:7px;cursor:default}
.crow:hover{background:#161616}
.cinfo{flex:1;min-width:0;cursor:pointer}
.cn{font-size:11.5px;color:#bbb;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cm{font-size:10px;color:#3a3a3a;margin-top:1px}
.ib{background:none;border:1px solid #252525;border-radius:4px;color:#555;padding:3px 7px;font-size:11px;cursor:pointer;line-height:1;flex-shrink:0}
.ib.p{border-color:#4a1a1a;color:#c0333a}
.ib.p:hover{background:#1e0a0a}
.ib.d:hover{border-color:#333;color:#999}

.ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:99;align-items:center;justify-content:center}
.ov.open{display:flex}
.mbox{background:#141414;border:1px solid #252525;border-radius:8px;padding:12px;width:min(380px,94vw);position:relative}
.mbox video{width:100%;border-radius:5px;display:block;max-height:68vh}
.mn{font-size:10px;color:#444;margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.xbtn{position:absolute;top:7px;right:9px;background:none;border:none;color:#444;font-size:16px;cursor:pointer;padding:2px 5px;line-height:1}
.xbtn:hover{color:#ccc}
</style>
</head>
<body>

<div class="topbar">
  <span class="logo">AsfaltTV</span>
  <div class="divider"></div>
  <div class="stat"><div class="v g" id="cams">-</div><div class="k">Canli</div></div>
  <div class="stat"><div class="v" id="clips">-</div><div class="k">Clip</div></div>
  <div class="stat"><div class="v y" id="q">-</div><div class="k">Kuyruk</div></div>
  <div class="stat"><div class="v" style="color:#555">{{ dur }}s</div><div class="k">Sure</div></div>
  <div class="spacer"></div>
  <label class="yt-lbl"><input type="checkbox" id="ytcb"> YouTube</label>
  <input class="cnt" type="number" id="cnt" value="3" min="1" max="20">
  <button class="run-btn" id="rbtn" onclick="go()">&#9654; Calistir</button>
  <div class="pill"><div class="dot" id="dot"></div><span id="st">Hazir</span></div>
</div>

<div class="body">
  <div class="log-wrap">
    <div class="ph"><span>Log</span><button class="clr" onclick="document.getElementById('log').innerHTML=''">temizle</button></div>
    <div class="log" id="log"></div>
  </div>
  <div class="clips-wrap">
    <div class="ph"><span>Clipler</span></div>
    <div class="clist" id="clist"></div>
  </div>
</div>

<div class="ov" id="ov" onclick="cx(event)">
  <div class="mbox">
    <button class="xbtn" onclick="cx()">&#x2715;</button>
    <div class="mn" id="mn"></div>
    <video id="mv" controls autoplay playsinline></video>
  </div>
</div>

<script>
let es=null;
const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

function parseLine(r){
  const m=r.match(/^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2},\\d+ (INFO|WARNING|ERROR)\\s+(.*)$/);
  if(!m)return{c:'m',t:r};
  const[,lv,t]=m;
  const c=lv==='WARNING'?'w':lv==='ERROR'?'e':
    /clip haz|yuklendi|basarili|tamamland/i.test(t)?'ok':'i';
  return{c,t};
}

function log(raw){
  const b=document.getElementById('log');
  const{c,t}=parseLine(raw);
  const d=document.createElement('div');
  d.className=c; d.textContent=t;
  b.appendChild(d);
  b.scrollTop=b.scrollHeight;
}

function setR(v){
  document.getElementById('rbtn').disabled=v;
  document.getElementById('st').textContent=v?'Calisiyor...':'Hazir';
  document.getElementById('dot').className='dot'+(v?' run':' ok');
  if(!v)stats();
}

function go(){
  const n=document.getElementById('cnt').value;
  const u=document.getElementById('ytcb').checked;
  setR(true);
  if(es)es.close();
  es=new EventSource('/run?count='+n+'&upload='+u);
  es.onmessage=e=>{
    if(e.data==='__DONE__'){es.close();setR(false);}
    else log(e.data);
  };
  es.onerror=()=>{es.close();setR(false);};
}

function stats(){
  fetch('/api/stats').then(r=>r.json()).then(d=>{
    document.getElementById('cams').textContent=d.cameras>=0?d.cameras:'?';
    document.getElementById('clips').textContent=d.clip_count;
    document.getElementById('q').textContent=d.queue_count;
    document.getElementById('clist').innerHTML=d.clips.map(c=>`
      <div class="crow">
        <div class="cinfo" onclick="pv('${esc(c.name)}')">
          <div class="cn">${esc(c.name.replace(/_/g,' ').replace('.mp4',''))}</div>
          <div class="cm">${c.modified} &middot; ${c.size_kb}KB</div>
        </div>
        <button class="ib p" onclick="pv('${esc(c.name)}')">&#9654;</button>
        <button class="ib d" onclick="dl('${esc(c.name)}',this)">&#x2715;</button>
      </div>`).join('');
  });
}

function pv(name){
  document.getElementById('mn').textContent=name;
  const v=document.getElementById('mv');
  v.src='/clips/'+encodeURIComponent(name);
  v.play();
  document.getElementById('ov').classList.add('open');
}

function cx(e){
  if(e&&!e.target.classList.contains('ov')&&!e.target.classList.contains('xbtn'))return;
  const v=document.getElementById('mv');
  v.pause();v.src='';
  document.getElementById('ov').classList.remove('open');
}

function dl(name,btn){
  if(!confirm(name+' silinsin mi?'))return;
  fetch('/clips/'+encodeURIComponent(name),{method:'DELETE'})
    .then(r=>r.json()).then(d=>{if(d.ok){btn.closest('.crow').remove();stats();}});
}

fetch('/api/logs').then(r=>r.json()).then(d=>d.lines.forEach(log));
stats();
setInterval(stats,15000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return render_template_string(TEMPLATE, dur=cfg["schedule"]["clip_duration"])


@app.route("/run")
def run_stream():
    count = int(request.args.get("count", 3))
    upload = request.args.get("upload", "false").lower() == "true"
    if not _pipeline_running.is_set():
        threading.Thread(target=_run_pipeline, args=(count, upload), daemon=True).start()

    def generate():
        while True:
            try:
                line = _log_queue.get(timeout=60)
                yield f"data: {line}\n\n"
                if line == "__DONE__":
                    break
            except queue.Empty:
                yield "data: \n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/stats")
def api_stats():
    return jsonify({
        "cameras": _active_cameras(),
        "clip_count": len(list(CLIPS_DIR.glob("*.mp4"))) if CLIPS_DIR.exists() else 0,
        "queue_count": _queue_count(),
        "clips": _clips_info(),
    })


@app.route("/api/logs")
def api_logs():
    return jsonify({"lines": _tail_log()})


@app.route("/clips/<filename>")
def serve_clip(filename):
    path = CLIPS_DIR / filename
    if not path.exists() or path.suffix != ".mp4":
        return "Not found", 404
    return send_file(str(path.resolve()), mimetype="video/mp4", conditional=True)


@app.route("/clips/<filename>", methods=["DELETE"])
def delete_clip(filename):
    path = CLIPS_DIR / filename
    if not path.exists() or path.suffix != ".mp4":
        return jsonify({"error": "not found"}), 404
    path.unlink()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("AsfaltTV Dashboard -> http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
