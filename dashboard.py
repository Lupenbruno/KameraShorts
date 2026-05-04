"""AsfaltTV Dashboard — Ankara + İstanbul pipeline yönetim paneli."""
import json
import queue
import subprocess
import sys
import threading
from datetime import datetime, date
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template_string, request, send_file

CONFIG_PATH  = Path("config.yaml")
CLIPS_DIR    = Path("data/clips")
IST_DIR      = Path("data/istanbul_clips")
LOG_PATH     = Path("logs/pipeline.log")
IST_LOG      = Path("logs/istanbul_pipeline.log")
QUEUE_PATH   = Path("data/queue/upload_queue.json")
IST_QUEUE    = Path("data/queue/istanbul_upload_queue.json")

app = Flask(__name__)
_anka_q:  queue.Queue = queue.Queue(maxsize=500)
_ist_q:   queue.Queue = queue.Queue(maxsize=500)
_running  = {"ankara": threading.Event(), "istanbul": threading.Event()}


# ── helpers ──────────────────────────────────────────────────────────────────

def _tail(path: Path, n=300) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


def _clips(d: Path, limit=40) -> list[dict]:
    if not d.exists():
        return []
    files = sorted(d.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        s = f.stat()
        out.append({
            "name": f.name,
            "size_mb": round(s.st_size / 1_048_576, 1),
            "modified": datetime.fromtimestamp(s.st_mtime).strftime("%d/%m %H:%M"),
            "ts": s.st_mtime,
        })
    return out


def _queue_len(p: Path) -> int:
    if not p.exists():
        return 0
    try:
        return len(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return 0


def _yt_today(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    today = date.today().isoformat()
    return sum(1 for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
               if today in ln and "UPLOADED" in ln)


def _ankara_cams() -> int:
    try:
        import requests as rq
        s = rq.Session()
        s.headers["User-Agent"] = "KameraShorts/1.0"
        data = s.get("https://seyret.ankara.bel.tr/status.json", timeout=8).json()
        return sum(1 for v in data if v.get("stream_url") and v.get("is_visible"))
    except Exception:
        return -1


def _next_run(times: list[str]) -> str:
    now = datetime.now()
    for t in sorted(times):
        h, m = map(int, t.split(":"))
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target > now:
            delta = target - now
            mins = int(delta.total_seconds() // 60)
            return f"{t} ({mins}dk sonra)"
    return times[0] + " (yarın)"


def _run_pipeline(script: str, count: int, upload: bool, q: queue.Queue, key: str):
    _running[key].set()
    cmd = [sys.executable, script, "--now", f"--count={count}"]
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
                    q.put_nowait(line)
                except queue.Full:
                    pass
        proc.wait()
    finally:
        _running[key].clear()
        try:
            q.put_nowait("__DONE__")
        except queue.Full:
            pass


# ── template ─────────────────────────────────────────────────────────────────

TEMPLATE = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AsfaltTV Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0e0e0e;--bg2:#141414;--bg3:#1a1a1a;--bg4:#202020;
  --border:#222;--border2:#2a2a2a;
  --text:#d0d0d0;--text2:#888;--text3:#444;
  --anka:#4a9eff;--ist:#2ecf8e;
  --red:#e63946;--yellow:#ffc107;--green:#4caf50;
}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;
     height:100vh;display:flex;flex-direction:column;overflow:hidden;font-size:13px}

/* ── topbar ── */
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);
        height:52px;padding:0 20px;display:flex;align-items:center;gap:14px;flex-shrink:0}
.logo{font-size:17px;font-weight:800;letter-spacing:-.3px;color:#fff}
.logo span{color:var(--red)}
.divider{width:1px;height:20px;background:var(--border2);flex-shrink:0}

/* ── stat cards ── */
.stats{display:flex;gap:10px;align-items:center}
.sc{background:var(--bg3);border:1px solid var(--border2);border-radius:7px;
    padding:5px 12px;display:flex;flex-direction:column;align-items:center;min-width:60px}
.sc .v{font-size:17px;font-weight:700;line-height:1.1}
.sc .k{font-size:9px;color:var(--text3);margin-top:2px;text-transform:uppercase;letter-spacing:.5px}
.sc .v.g{color:var(--green)}.sc .v.y{color:var(--yellow)}.sc .v.b{color:var(--anka)}.sc .v.t{color:var(--ist)}

/* quota bar */
.qbar-wrap{display:flex;flex-direction:column;align-items:center;gap:3px;min-width:56px}
.qbar-track{width:50px;height:5px;background:var(--border2);border-radius:3px;overflow:hidden}
.qbar-fill{height:100%;border-radius:3px;background:var(--green);transition:width .4s}
.qbar-fill.warn{background:var(--yellow)}.qbar-fill.full{background:var(--red)}
.qbar-lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}

.spacer{flex:1}
.pill{font-size:11px;color:var(--text2);display:flex;align-items:center;gap:5px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--border2);transition:all .3s}
.dot.run{background:var(--red);animation:bl 1s infinite}.dot.ok{background:var(--green)}
@keyframes bl{0%,100%{opacity:1}50%{opacity:.1}}

/* ── pipeline tabs ── */
.tabs{display:flex;gap:1px;background:var(--border);border-radius:7px;overflow:hidden;flex-shrink:0}
.tab{padding:5px 14px;font-size:12px;font-weight:600;cursor:pointer;transition:all .2s;
     background:var(--bg3);color:var(--text3);border:none;white-space:nowrap}
.tab.a-active{background:rgba(74,158,255,.15);color:var(--anka)}
.tab.i-active{background:rgba(46,207,142,.15);color:var(--ist)}
.tab:hover:not(.a-active):not(.i-active){background:var(--bg4);color:var(--text)}

/* controls */
.ctrl{display:flex;align-items:center;gap:8px;flex-shrink:0}
.cnt{width:44px;background:var(--bg3);border:1px solid var(--border2);color:var(--text);
     padding:5px 6px;border-radius:5px;font-size:12px;text-align:center}
.yt-lbl{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text2);cursor:pointer}
.yt-lbl input{accent-color:var(--red);cursor:pointer}
.run-btn{border:none;border-radius:6px;padding:7px 16px;font-size:12px;font-weight:700;cursor:pointer;
         color:#fff;transition:all .2s;white-space:nowrap}
.run-btn.anka{background:var(--anka)}.run-btn.anka:hover:not(:disabled){background:#2f85e8}
.run-btn.ist{background:var(--ist)}.run-btn.ist:hover:not(:disabled){background:#1fb87a}
.run-btn:disabled{opacity:.3;cursor:not-allowed}

/* ── body layout ── */
.body{display:grid;grid-template-columns:1fr 1fr 300px;flex:1;min-height:0}

/* ── panels ── */
.panel{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--border)}
.ph{padding:7px 14px;border-bottom:1px solid var(--border);font-size:10px;font-weight:700;
    color:var(--text3);text-transform:uppercase;letter-spacing:.6px;display:flex;
    justify-content:space-between;align-items:center;flex-shrink:0}
.ph .badge{font-size:10px;padding:1px 7px;border-radius:10px;font-weight:700}
.badge.anka{background:rgba(74,158,255,.15);color:var(--anka)}
.badge.ist{background:rgba(46,207,142,.15);color:var(--ist)}
.ph-btn{background:none;border:1px solid var(--border2);color:var(--text3);border-radius:3px;
        padding:1px 7px;font-size:9px;cursor:pointer}
.ph-btn:hover{color:var(--text2);border-color:#333}

/* ── log ── */
.log{flex:1;overflow-y:auto;padding:9px 14px;font-family:Consolas,'Cascadia Code',monospace;
     font-size:11px;line-height:1.8;color:var(--text3)}
.log .i{color:#3d6e8a}.log .ok{color:#3a7a45}.log .w{color:#8a6830}.log .e{color:#8a3030}
.log .m{color:#555}.log .src-anka{color:rgba(74,158,255,.7)}.log .src-ist{color:rgba(46,207,142,.7)}
.log-empty{color:var(--text3);font-size:11px;padding:20px;text-align:center;font-family:inherit}

/* ── clips panel ── */
.ctabs{display:flex;border-bottom:1px solid var(--border);flex-shrink:0}
.ctab{flex:1;padding:7px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
      cursor:pointer;text-align:center;border:none;background:none;color:var(--text3);
      border-bottom:2px solid transparent;transition:all .2s}
.ctab.ca{color:var(--anka);border-bottom-color:var(--anka)}
.ctab.ci{color:var(--ist);border-bottom-color:var(--ist)}
.clist{flex:1;overflow-y:auto}
.crow{padding:7px 12px;border-bottom:1px solid #191919;display:flex;align-items:center;gap:7px}
.crow:hover{background:var(--bg3)}
.cinfo{flex:1;min-width:0;cursor:pointer}
.cn{font-size:11.5px;color:#bbb;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cm{font-size:10px;color:var(--text3);margin-top:2px}
.city-tag{font-size:9px;padding:1px 5px;border-radius:3px;font-weight:700;flex-shrink:0}
.city-tag.a{background:rgba(74,158,255,.12);color:var(--anka)}
.city-tag.i{background:rgba(46,207,142,.12);color:var(--ist)}
.ib{background:none;border:1px solid var(--border2);border-radius:4px;color:var(--text3);
    padding:3px 7px;font-size:11px;cursor:pointer;line-height:1;flex-shrink:0}
.ib.p:hover{background:#1e0a0a;border-color:#4a1a1a;color:#c0333a}
.ib.d:hover{border-color:#333;color:#999}

/* ── modal ── */
.ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:99;
    align-items:center;justify-content:center}
.ov.open{display:flex}
.mbox{background:#141414;border:1px solid #252525;border-radius:10px;padding:14px;
      width:min(460px,95vw);position:relative}
.mbox video{width:100%;border-radius:6px;display:block;max-height:70vh;background:#000}
.mn{font-size:10px;color:var(--text3);margin-bottom:9px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.xbtn{position:absolute;top:8px;right:10px;background:none;border:none;color:var(--text3);
      font-size:17px;cursor:pointer;padding:2px 5px;line-height:1}
.xbtn:hover{color:#ccc}

/* ── schedule ribbon ── */
.sched{background:var(--bg2);border-top:1px solid var(--border);padding:5px 20px;
       display:flex;align-items:center;gap:6px;flex-shrink:0;overflow-x:auto}
.sched-lbl{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;
           white-space:nowrap;margin-right:4px}
.stime{font-size:10px;font-weight:600;padding:2px 9px;border-radius:10px;
       background:var(--bg3);color:var(--text3);border:1px solid var(--border2);white-space:nowrap}
.stime.next{background:rgba(230,57,70,.15);color:var(--red);border-color:rgba(230,57,70,.3)}
.stime.past{opacity:.3}
.sched-spacer{flex:1}
.sched-next{font-size:10px;color:var(--text2);white-space:nowrap}

/* scrollbars */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:2px}
::-webkit-scrollbar-thumb:hover{background:#3a3a3a}
</style>
</head>
<body>

<!-- ── TOPBAR ── -->
<div class="topbar">
  <div class="logo">Asfalt<span>TV</span></div>
  <div class="divider"></div>

  <div class="stats">
    <div class="sc"><div class="v g" id="anka-cams">-</div><div class="k">Ankara</div></div>
    <div class="sc"><div class="v t">21</div><div class="k">İstanbul</div></div>
    <div class="sc"><div class="v b" id="total-clips">-</div><div class="k">Klip</div></div>
    <div class="sc"><div class="v y" id="queue-total">-</div><div class="k">Kuyruk</div></div>
    <div class="sc">
      <div class="qbar-wrap">
        <div class="k" style="margin-bottom:3px">YT Kota</div>
        <div class="qbar-track"><div class="qbar-fill" id="qbar" style="width:0%"></div></div>
        <div style="font-size:9px;color:var(--text3);margin-top:2px" id="yt-used">0/6</div>
      </div>
    </div>
  </div>

  <div class="divider"></div>

  <!-- Ankara pipeline -->
  <div class="tabs">
    <button class="tab a-active" id="tab-anka" onclick="selPipe('ankara')">🚌 Ankara</button>
    <button class="tab" id="tab-ist" onclick="selPipe('istanbul')">🌉 İstanbul</button>
  </div>

  <div class="ctrl">
    <label class="yt-lbl"><input type="checkbox" id="ytcb"> YouTube</label>
    <input class="cnt" type="number" id="cnt" value="1" min="1" max="10">
    <button class="run-btn anka" id="rbtn" onclick="go()">▶ Çalıştır</button>
    <div class="pill"><div class="dot" id="dot"></div><span id="st">Hazır</span></div>
  </div>
</div>

<!-- ── MAIN BODY ── -->
<div class="body">

  <!-- Ankara log -->
  <div class="panel">
    <div class="ph">
      <span>Log <span class="badge anka">Ankara</span></span>
      <button class="ph-btn" onclick="document.getElementById('log-a').innerHTML=''">temizle</button>
    </div>
    <div class="log" id="log-a">
      <div class="log-empty">Pipeline çalıştırıldığında loglar burada görünür</div>
    </div>
  </div>

  <!-- İstanbul log -->
  <div class="panel">
    <div class="ph">
      <span>Log <span class="badge ist">İstanbul</span></span>
      <button class="ph-btn" onclick="document.getElementById('log-i').innerHTML=''">temizle</button>
    </div>
    <div class="log" id="log-i">
      <div class="log-empty">Pipeline çalıştırıldığında loglar burada görünür</div>
    </div>
  </div>

  <!-- Clips panel -->
  <div class="panel" style="border-right:none">
    <div class="ctabs">
      <button class="ctab ca" id="ctab-a" onclick="selClipTab('a')">🚌 Ankara</button>
      <button class="ctab" id="ctab-i" onclick="selClipTab('i')">🌉 İstanbul</button>
    </div>
    <div class="clist" id="clist-a"></div>
    <div class="clist" id="clist-i" style="display:none"></div>
  </div>

</div>

<!-- ── SCHEDULE RIBBON ── -->
<div class="sched">
  <div class="sched-lbl">Program</div>
  <div id="sched-pills"></div>
  <div class="sched-spacer"></div>
  <div class="sched-next" id="sched-next"></div>
</div>

<!-- ── VIDEO MODAL ── -->
<div class="ov" id="ov" onclick="cx(event)">
  <div class="mbox">
    <button class="xbtn" onclick="cx()">&#x2715;</button>
    <div class="mn" id="mn"></div>
    <video id="mv" controls autoplay playsinline></video>
  </div>
</div>

<script>
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
let curPipe = 'ankara';
let esAnka = null, esIst = null;
let curClipTab = 'a';

// ── pipeline selector ──
function selPipe(p) {
  curPipe = p;
  document.getElementById('tab-anka').className = 'tab' + (p==='ankara'?' a-active':'');
  document.getElementById('tab-ist').className  = 'tab' + (p==='istanbul'?' i-active':'');
  const btn = document.getElementById('rbtn');
  btn.className = 'run-btn ' + (p==='ankara'?'anka':'ist');
  btn.disabled = p === 'ankara' ? _ankaBusy : _istBusy;
}

// ── clip tab selector ──
function selClipTab(t) {
  curClipTab = t;
  document.getElementById('ctab-a').className = 'ctab' + (t==='a'?' ca':'');
  document.getElementById('ctab-i').className = 'ctab' + (t==='i'?' ci':'');
  document.getElementById('clist-a').style.display = t==='a'?'':'none';
  document.getElementById('clist-i').style.display = t==='i'?'':'none';
}

// ── log parser ──
function parseLine(r) {
  const m = r.match(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ (INFO|WARNING|ERROR)\s+(.*)$/);
  if (!m) return {c:'m', t:r};
  const [,lv,t] = m;
  const c = lv==='WARNING'?'w' : lv==='ERROR'?'e' :
    /clip haz|yuklendi|basarili|tamamland|ses eklendi/i.test(t)?'ok':'i';
  return {c, t};
}

function logLine(raw, el) {
  const b = document.getElementById(el);
  // remove placeholder
  const empty = b.querySelector('.log-empty');
  if (empty) empty.remove();
  const {c,t} = parseLine(raw);
  const d = document.createElement('div');
  d.className = c; d.textContent = t;
  b.appendChild(d);
  b.scrollTop = b.scrollHeight;
}

// ── running state ──
let _ankaBusy = false, _istBusy = false;
function setR(key, v) {
  if (key==='ankara') _ankaBusy = v;
  else _istBusy = v;
  if (curPipe===key) {
    document.getElementById('rbtn').disabled = v;
    document.getElementById('st').textContent = v ? 'Çalışıyor...' : 'Hazır';
    document.getElementById('dot').className = 'dot' + (v?' run':' ok');
  }
  if (!v) stats();
}

// ── run ──
function go() {
  const n   = document.getElementById('cnt').value;
  const u   = document.getElementById('ytcb').checked;
  const key = curPipe;
  const logEl = key==='ankara' ? 'log-a' : 'log-i';
  setR(key, true);

  if (key==='ankara') {
    if (esAnka) esAnka.close();
    esAnka = new EventSource('/run/ankara?count='+n+'&upload='+u);
    esAnka.onmessage = e => {
      if (e.data==='__DONE__') { esAnka.close(); setR('ankara', false); }
      else logLine(e.data, logEl);
    };
    esAnka.onerror = () => { esAnka.close(); setR('ankara', false); };
  } else {
    if (esIst) esIst.close();
    esIst = new EventSource('/run/istanbul?count='+n+'&upload='+u);
    esIst.onmessage = e => {
      if (e.data==='__DONE__') { esIst.close(); setR('istanbul', false); }
      else logLine(e.data, logEl);
    };
    esIst.onerror = () => { esIst.close(); setR('istanbul', false); };
  }
}

// ── stats ──
function stats() {
  fetch('/api/stats').then(r=>r.json()).then(d => {
    const cams = d.ankara_cams;
    document.getElementById('anka-cams').textContent = cams >= 0 ? cams : '?';
    document.getElementById('total-clips').textContent = d.clip_count_a + d.clip_count_i;
    document.getElementById('queue-total').textContent = d.queue_a + d.queue_i;

    const used = d.yt_used_a;
    const pct  = Math.min(100, (used/6)*100);
    const bar  = document.getElementById('qbar');
    bar.style.width = pct + '%';
    bar.className   = 'qbar-fill' + (pct>=100?' full':pct>=66?' warn':'');
    document.getElementById('yt-used').textContent = used + '/6';

    // clips
    renderClips('clist-a', d.clips_a, 'a');
    renderClips('clist-i', d.clips_i, 'i');
  });
}

function renderClips(elId, clips, tag) {
  const el = document.getElementById(elId);
  if (!clips || !clips.length) {
    el.innerHTML = '<div style="color:var(--text3);font-size:11px;padding:20px;text-align:center">Henüz klip yok</div>';
    return;
  }
  el.innerHTML = clips.map(c => {
    const dir = tag==='a' ? 'ankara' : 'istanbul';
    const nice = c.name.replace(/_/g,' ').replace('.mp4','');
    return `<div class="crow">
      <div class="cinfo" onclick="pv('${esc(c.name)}','${dir}')">
        <div class="cn">${esc(nice)}</div>
        <div class="cm">${c.modified} &middot; ${c.size_mb}MB</div>
      </div>
      <button class="ib p" onclick="pv('${esc(c.name)}','${dir}')">▶</button>
      <button class="ib d" onclick="dlClip('${esc(c.name)}','${dir}',this)">✕</button>
    </div>`;
  }).join('');
}

// ── schedule pills ──
function renderSchedule(times) {
  const now = new Date();
  const nowMins = now.getHours()*60 + now.getMinutes();
  let nextSet = false;
  const pills = times.map(t => {
    const [h,m] = t.split(':').map(Number);
    const tMins = h*60+m;
    let cls = 'stime';
    if (tMins < nowMins && !nextSet) { cls += ' past'; }
    else if (tMins >= nowMins && !nextSet) { cls += ' next'; nextSet = true; }
    else if (tMins < nowMins) { cls += ' past'; }
    return `<span class="${cls}">${t}</span>`;
  });
  document.getElementById('sched-pills').innerHTML = pills.join(' ');
}

// ── video modal ──
function pv(name, dir) {
  document.getElementById('mn').textContent = name;
  const v = document.getElementById('mv');
  v.src = '/clips/' + dir + '/' + encodeURIComponent(name);
  v.play();
  document.getElementById('ov').classList.add('open');
}
function cx(e) {
  if (e && !e.target.classList.contains('ov') && !e.target.classList.contains('xbtn')) return;
  const v = document.getElementById('mv');
  v.pause(); v.src = '';
  document.getElementById('ov').classList.remove('open');
}
function dlClip(name, dir, btn) {
  if (!confirm(name + '\nsilinsin mi?')) return;
  fetch('/clips/'+dir+'/'+encodeURIComponent(name), {method:'DELETE'})
    .then(r=>r.json()).then(d=>{ if(d.ok){ btn.closest('.crow').remove(); stats(); }});
}

// ── init ──
fetch('/api/logs/ankara').then(r=>r.json()).then(d => {
  d.lines.forEach(l => logLine(l, 'log-a'));
});
fetch('/api/logs/istanbul').then(r=>r.json()).then(d => {
  d.lines.forEach(l => logLine(l, 'log-i'));
});
fetch('/api/schedule').then(r=>r.json()).then(d => renderSchedule(d.times));

stats();
setInterval(stats, 15000);
</script>
</body>
</html>
"""


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(TEMPLATE)


@app.route("/run/<pipeline>")
def run_stream(pipeline):
    if pipeline not in ("ankara", "istanbul"):
        return "Not found", 404
    count  = int(request.args.get("count", 1))
    upload = request.args.get("upload", "false").lower() == "true"
    script = "main.py" if pipeline == "ankara" else "istanbul_main.py"
    q      = _anka_q if pipeline == "ankara" else _ist_q

    if not _running[pipeline].is_set():
        threading.Thread(
            target=_run_pipeline,
            args=(script, count, upload, q, pipeline),
            daemon=True,
        ).start()

    def generate():
        while True:
            try:
                line = q.get(timeout=90)
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
        "ankara_cams":  _ankara_cams(),
        "clip_count_a": len(list(CLIPS_DIR.glob("*.mp4"))) if CLIPS_DIR.exists() else 0,
        "clip_count_i": len(list(IST_DIR.glob("*.mp4")))   if IST_DIR.exists()   else 0,
        "queue_a":      _queue_len(QUEUE_PATH),
        "queue_i":      _queue_len(IST_QUEUE),
        "yt_used_a":    _yt_today(LOG_PATH) + _yt_today(IST_LOG),
        "clips_a":      _clips(CLIPS_DIR),
        "clips_i":      _clips(IST_DIR),
    })


@app.route("/api/logs/<pipeline>")
def api_logs(pipeline):
    path = LOG_PATH if pipeline == "ankara" else IST_LOG
    return jsonify({"lines": _tail(path, 150)})


@app.route("/api/schedule")
def api_schedule():
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        times = cfg["schedule"]["times"]
    except Exception:
        times = ["06:00","09:00","12:00","15:00","18:00","21:00"]
    return jsonify({"times": times})


@app.route("/clips/<city>/<filename>")
def serve_clip(city, filename):
    base = CLIPS_DIR if city == "ankara" else IST_DIR
    path = base / filename
    if not path.exists() or path.suffix != ".mp4":
        return "Not found", 404
    return send_file(str(path.resolve()), mimetype="video/mp4", conditional=True)


@app.route("/clips/<city>/<filename>", methods=["DELETE"])
def delete_clip(city, filename):
    base = CLIPS_DIR if city == "ankara" else IST_DIR
    path = base / filename
    if not path.exists() or path.suffix != ".mp4":
        return jsonify({"error": "not found"}), 404
    path.unlink()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("AsfaltTV Dashboard -> http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
