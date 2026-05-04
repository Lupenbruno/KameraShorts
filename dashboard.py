"""AsfaltTV — Basit kontrol paneli."""
import json, subprocess, sys, threading, time, base64, functools
from datetime import datetime, date
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template_string, send_file, request

CONFIG_PATH = Path("config.yaml")
LOG_A = Path("logs/pipeline.log")
LOG_I = Path("logs/istanbul_pipeline.log")
CLIPS_A = Path("data/clips")
CLIPS_I = Path("data/istanbul_clips")

# --- Giriş bilgileri ---
DASHBOARD_USER = "asfalt"
DASHBOARD_PASS = "Asfalt2026!"

app = Flask(__name__)
_daemons = {"ankara": None, "istanbul": None}


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, pw = decoded.split(":", 1)
                if user == DASHBOARD_USER and pw == DASHBOARD_PASS:
                    return f(*args, **kwargs)
            except Exception:
                pass
        return Response(
            "Giris yapmaniz gerekiyor.",
            401,
            {"WWW-Authenticate": 'Basic realm="AsfaltTV"'}
        )
    return decorated


# Tüm route'lara auth uygula
@app.before_request
def check_auth():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            user, pw = decoded.split(":", 1)
            if user == DASHBOARD_USER and pw == DASHBOARD_PASS:
                return None
        except Exception:
            pass
    return Response(
        "Giris yapmaniz gerekiyor.",
        401,
        {"WWW-Authenticate": 'Basic realm="AsfaltTV"'}
    )


def _cfg():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

def _daemon_alive(key):
    p = _daemons[key]
    return p is not None and p.poll() is None

def _start_daemon(key):
    if _daemon_alive(key):
        return
    script = "main.py" if key == "ankara" else "istanbul_main.py"
    _daemons[key] = subprocess.Popen(
        [sys.executable, script, "--daemon"],
        cwd=str(Path(__file__).parent)
    )

def _stop_daemon(key):
    p = _daemons[key]
    if p and p.poll() is None:
        p.terminate()
        try: p.wait(timeout=5)
        except: p.kill()
    _daemons[key] = None

def _yt_today(log_path):
    if not log_path.exists(): return []
    today = date.today().isoformat()
    out = []
    for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if today in ln and "UPLOADED" in ln:
            # "2026-05-04T10:03:00 UPLOADED abc123 | Başlık"
            parts = ln.split("UPLOADED", 1)
            if len(parts) == 2:
                rest = parts[1].strip()
                vid_id = rest.split("|")[0].strip()
                title = rest.split("|")[1].strip() if "|" in rest else ""
                out.append({"url": f"https://youtube.com/watch?v={vid_id}", "title": title})
    return out

def _tail(path, n=80):
    if not path.exists(): return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]

def _next_run(times):
    now = datetime.now()
    nm = now.hour * 60 + now.minute
    for t in sorted(times):
        h, m = map(int, t.split(":"))
        diff = h * 60 + m - nm
        if diff > 0:
            return t, diff
    h, m = map(int, sorted(times)[0].split(":"))
    diff = (24 * 60 - nm) + h * 60 + m
    return sorted(times)[0], diff

def _schedule_status(times):
    now = datetime.now()
    nm = now.hour * 60 + now.minute
    result = []
    for t in sorted(times):
        h, m = map(int, t.split(":"))
        tm = h * 60 + m
        result.append({"time": t, "past": tm < nm, "current": abs(tm - nm) <= 30})
    return result


TMPL = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AsfaltTV</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0e0e0e;color:#d0d0d0;font-family:'Segoe UI',system-ui,sans-serif;
     min-height:100vh;font-size:14px}

.header{background:#141414;border-bottom:1px solid #222;padding:0 24px;
        height:54px;display:flex;align-items:center;gap:16px}
.logo{font-size:18px;font-weight:800;color:#fff}.logo b{color:#e63946}
.spacer{flex:1}
.next-badge{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:20px;
            padding:5px 14px;font-size:12px;color:#888}
.next-badge b{color:#ffc107}

.main{max-width:960px;margin:0 auto;padding:28px 20px;display:flex;flex-direction:column;gap:24px}

/* sistem başlatma kartı */
.start-card{background:#141414;border:1px solid #222;border-radius:12px;padding:28px;
            text-align:center}
.start-card h2{font-size:20px;font-weight:700;margin-bottom:8px;color:#fff}
.start-card p{color:#666;font-size:13px;margin-bottom:22px;line-height:1.6}
.big-btn{display:inline-flex;align-items:center;gap:10px;background:#e63946;color:#fff;
         border:none;border-radius:8px;padding:13px 32px;font-size:15px;font-weight:700;
         cursor:pointer;transition:all .2s}
.big-btn:hover{background:#c8303c;transform:translateY(-1px)}
.big-btn:disabled{background:#333;color:#666;cursor:not-allowed;transform:none}
.stop-btn{background:#1e1e1e;border:1px solid #333;color:#888;border-radius:8px;
          padding:10px 24px;font-size:13px;font-weight:600;cursor:pointer;transition:all .2s}
.stop-btn:hover{border-color:#555;color:#bbb}

/* durum kartları */
.cards{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:#141414;border:1px solid #222;border-radius:10px;padding:18px}
.card-head{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.card-icon{font-size:22px}
.card-title{font-size:15px;font-weight:700;color:#fff}
.card-sub{font-size:11px;color:#444;margin-top:2px}
.status-row{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.on{background:#4caf50;box-shadow:0 0 6px #4caf5055;animation:pulse 2s infinite}
.dot.off{background:#333}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.status-txt{font-size:13px;font-weight:600}
.status-txt.on{color:#4caf50}
.status-txt.off{color:#555}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
.stat{background:#1a1a1a;border-radius:6px;padding:8px 10px;text-align:center}
.stat .v{font-size:20px;font-weight:700;color:#fff}
.stat .k{font-size:9px;color:#444;text-transform:uppercase;letter-spacing:.5px;margin-top:2px}

/* program */
.sched-card{background:#141414;border:1px solid #222;border-radius:10px;padding:18px}
.sched-card h3{font-size:13px;font-weight:600;color:#666;text-transform:uppercase;
               letter-spacing:.5px;margin-bottom:14px}
.sched-times{display:flex;gap:8px;flex-wrap:wrap}
.st{padding:6px 14px;border-radius:20px;font-size:13px;font-weight:600;
    background:#1a1a1a;color:#444;border:1px solid #222}
.st.past{opacity:.4}
.st.active{background:rgba(76,175,80,.15);color:#4caf50;border-color:rgba(76,175,80,.3)}
.st.next{background:rgba(255,193,7,.1);color:#ffc107;border-color:rgba(255,193,7,.25)}

/* bugun yuklenenler */
.uploads-card{background:#141414;border:1px solid #222;border-radius:10px;padding:18px}
.uploads-card h3{font-size:13px;font-weight:600;color:#666;text-transform:uppercase;
                 letter-spacing:.5px;margin-bottom:14px}
.ulist{display:flex;flex-direction:column;gap:6px}
.uitem{display:flex;align-items:center;gap:10px;padding:9px 12px;background:#1a1a1a;
       border-radius:6px;font-size:12px}
.uitem .utag{font-size:10px;padding:2px 7px;border-radius:3px;font-weight:700;flex-shrink:0}
.utag.a{background:rgba(74,158,255,.12);color:#4a9eff}
.utag.i{background:rgba(46,207,142,.12);color:#2ecf8e}
.uitem .utitle{flex:1;color:#bbb;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.uitem a{color:#e63946;font-size:11px;text-decoration:none;flex-shrink:0}
.uitem a:hover{text-decoration:underline}
.empty-msg{color:#444;font-size:12px;padding:12px 0;text-align:center}

/* log */
.log-card{background:#141414;border:1px solid #222;border-radius:10px;padding:18px}
.log-card h3{font-size:13px;font-weight:600;color:#666;text-transform:uppercase;
             letter-spacing:.5px;margin-bottom:12px;display:flex;justify-content:space-between}
.log-tabs{display:flex;gap:2px;margin-bottom:12px}
.ltab{padding:4px 14px;font-size:11px;font-weight:600;cursor:pointer;border:none;
      background:#1a1a1a;color:#444;border-radius:4px}
.ltab.act-a{background:rgba(74,158,255,.15);color:#4a9eff}
.ltab.act-i{background:rgba(46,207,142,.15);color:#2ecf8e}
.logbox{background:#0e0e0e;border-radius:6px;padding:12px;height:180px;overflow-y:auto;
        font-family:Consolas,monospace;font-size:11px;line-height:1.7}
.logbox .i{color:#3d6e8a}.logbox .ok{color:#3a7a45}.logbox .w{color:#8a6830}
.logbox .e{color:#8a3030}.logbox .m{color:#444}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:2px}
</style>
</head>
<body>

<div class="header">
  <div class="logo">Asfalt<b>TV</b></div>
  <div class="spacer"></div>
  <div class="next-badge">Sonraki yayın: <b id="next-time">—</b></div>
</div>

<div class="main">

  <!-- BAŞLATMA KARTI -->
  <div class="start-card" id="start-card">
    <h2>Sistemi Başlat</h2>
    <p>Her gün 6 vakit Ankara otobüs klibi (30 sn · Shorts)<br>
       ve 6 vakit İstanbul manzara videosu (3 dk) otomatik yüklenecek.</p>
    <div style="display:flex;gap:16px;justify-content:center;flex-wrap:wrap;margin-bottom:10px">
      <div style="display:flex;flex-direction:column;align-items:center;gap:8px">
        <button class="big-btn" id="btn-start-a" style="background:#2d6fbf">▶ Ankara Başlat</button>
        <button class="stop-btn" id="btn-stop-a" style="display:none">⏹ Ankara Durdur</button>
        <span id="lbl-a" style="font-size:11px;color:#444">Durdu</span>
      </div>
      <div style="display:flex;flex-direction:column;align-items:center;gap:8px">
        <button class="big-btn" id="btn-start-i">▶ İstanbul Başlat</button>
        <button class="stop-btn" id="btn-stop-i" style="display:none">⏹ İstanbul Durdur</button>
        <span id="lbl-i" style="font-size:11px;color:#444">Durdu</span>
      </div>
    </div>
    <div style="margin-top:4px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
      <button class="stop-btn" id="btn-start-all" style="font-size:12px;padding:8px 20px">▶▶ Her İkisini Başlat</button>
      <button class="stop-btn" id="btn-test" style="font-size:12px;padding:8px 20px;border-color:#ffc107;color:#ffc107">⚡ Test</button>
      <button class="stop-btn" id="btn-test-a" style="font-size:12px;padding:8px 20px;border-color:#4a9eff;color:#4a9eff">🚌 Ankara Test Kaydı</button>
      <button class="stop-btn" id="btn-test-i" style="font-size:12px;padding:8px 20px;border-color:#2ecf8e;color:#2ecf8e">🌉 İstanbul Test Kaydı</button>
    </div>
    <div id="test-result" style="margin-top:12px;font-size:12px;color:#444;line-height:1.6"></div>
  </div>

  <!-- DURUM KARTLARI -->
  <div class="cards">
    <div class="card">
      <div class="card-head">
        <div class="card-icon">🚌</div>
        <div>
          <div class="card-title">Ankara</div>
          <div class="card-sub">EGO otobüs kameraları · 30sn · Dikey</div>
        </div>
      </div>
      <div class="status-row">
        <div class="dot off" id="dot-a"></div>
        <div class="status-txt off" id="stxt-a">Durdu</div>
      </div>
      <div class="stat-grid">
        <div class="stat"><div class="v" id="yt-a">0</div><div class="k">Bugün YT</div></div>
        <div class="stat"><div class="v" id="clips-a">0</div><div class="k">Klip</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">
        <div class="card-icon">🌉</div>
        <div>
          <div class="card-title">İstanbul</div>
          <div class="card-sub">Turistik kameralar · 3dk · Yatay</div>
        </div>
      </div>
      <div class="status-row">
        <div class="dot off" id="dot-i"></div>
        <div class="status-txt off" id="stxt-i">Durdu</div>
      </div>
      <div class="stat-grid">
        <div class="stat"><div class="v" id="yt-i">0</div><div class="k">Bugün YT</div></div>
        <div class="stat"><div class="v" id="clips-i">0</div><div class="k">Klip</div></div>
      </div>
    </div>
  </div>

  <!-- PROGRAM -->
  <div class="sched-card">
    <h3>Günlük Program</h3>
    <div class="sched-times" id="sched-times"></div>
  </div>

  <!-- BUGÜN YÜKLENDİ -->
  <div class="uploads-card">
    <h3>Bugün Yüklenenler</h3>
    <div class="ulist" id="ulist">
      <div class="empty-msg">Henüz yükleme yok</div>
    </div>
  </div>

  <!-- KLİPLER -->
  <div class="uploads-card">
    <h3>Son Kaydedilen Klipler</h3>
    <div class="ulist" id="clips-list">
      <div class="empty-msg">Klip bulunamadı</div>
    </div>
  </div>

  <!-- LOG -->
  <div class="log-card">
    <h3>Log</h3>
    <div class="log-tabs">
      <button class="ltab act-a" id="ltab-a" onclick="selLog('a')">🚌 Ankara</button>
      <button class="ltab" id="ltab-i" onclick="selLog('i')">🌉 İstanbul</button>
    </div>
    <div class="logbox" id="log-a"></div>
    <div class="logbox" id="log-i" style="display:none"></div>
  </div>

</div>

<script>
// --- buton event listener'ları ---
document.addEventListener('DOMContentLoaded', function() {
  document.getElementById('btn-start-a').addEventListener('click', function() {
    fetch('/api/daemon/start/ankara', {method:'POST'}).then(function() { setTimeout(loadStats, 1000); });
  });
  document.getElementById('btn-stop-a').addEventListener('click', function() {
    if (!confirm('Ankara durdurulsun mu?')) return;
    fetch('/api/daemon/stop/ankara', {method:'POST'}).then(function() { setTimeout(loadStats, 1000); });
  });
  document.getElementById('btn-start-i').addEventListener('click', function() {
    fetch('/api/daemon/start/istanbul', {method:'POST'}).then(function() { setTimeout(loadStats, 1000); });
  });
  document.getElementById('btn-stop-i').addEventListener('click', function() {
    if (!confirm('Istanbul durdurulsun mu?')) return;
    fetch('/api/daemon/stop/istanbul', {method:'POST'}).then(function() { setTimeout(loadStats, 1000); });
  });
  document.getElementById('btn-start-all').addEventListener('click', function() {
    fetch('/api/daemon/start/ankara', {method:'POST'});
    fetch('/api/daemon/start/istanbul', {method:'POST'});
    setTimeout(loadStats, 1000);
  });
  document.getElementById('btn-test').addEventListener('click', function() {
    var r = document.getElementById('test-result');
    r.style.color = '#ffc107';
    r.textContent = 'JavaScript calisiyor, API test ediliyor...';
    fetch('/api/status').then(function(res) { return res.json(); }).then(function(d) {
      r.style.color = '#4caf50';
      r.textContent = 'SISTEM OK — Ankara: ' + (d.daemon_ankara ? 'Calisiyor' : 'Durdu') + ' | Istanbul: ' + (d.daemon_istanbul ? 'Calisiyor' : 'Durdu');
    }).catch(function(e) {
      r.style.color = '#e63946';
      r.textContent = 'API HATASI: ' + e;
    });
  });

  function testRecord(pipe) {
    var r = document.getElementById('test-result');
    var btn = document.getElementById('btn-test-' + (pipe==='ankara'?'a':'i'));
    btn.disabled = true;
    btn.textContent = '⏳ Kayıt alınıyor...';
    r.style.cssText = 'margin-top:12px;font-size:11px;color:#aaa;line-height:1.8;text-align:left;background:#0e0e0e;padding:10px;border-radius:6px;max-height:200px;overflow-y:auto;font-family:Consolas,monospace';
    r.textContent = '';

    var es = new EventSource('/api/test/record/' + pipe);
    es.onmessage = function(e) {
      var msg = e.data;
      if (msg.startsWith('__CLIP__')) {
        es.close();
        btn.disabled = false;
        btn.textContent = pipe==='ankara' ? '🚌 Ankara Test Kaydı' : '🌉 İstanbul Test Kaydı';
        var clipPath = msg.replace('__CLIP__','');
        var fname = clipPath.split('\\').pop();
        var div = document.createElement('div');
        div.style.cssText = 'margin-top:8px;padding:8px;background:#1a2e1a;border-radius:4px;color:#4caf50;font-family:sans-serif';
        div.innerHTML = '✅ <b>KAYIT BAŞARILI:</b> ' + fname + ' &nbsp;'
          + '<button onclick="openInExplorer(\'' + clipPath.replace(/\\/g,'\\\\') + '\')" '
          + 'style="background:#4caf50;color:#fff;border:none;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px">📁 Klasörde Aç</button>';
        r.appendChild(div);
        r.scrollTop = r.scrollHeight;
        loadClips();
      } else if (msg.startsWith('__FAIL__')) {
        es.close();
        btn.disabled = false;
        btn.textContent = pipe==='ankara' ? '🚌 Ankara Test Kaydı' : '🌉 İstanbul Test Kaydı';
        var div = document.createElement('div');
        div.style.color = '#e63946';
        div.textContent = '❌ ' + msg.replace('__FAIL__','');
        r.appendChild(div);
      } else {
        var line = document.createElement('div');
        line.style.color = msg.includes('HATA') || msg.includes('hata') ? '#e63946'
                         : msg.includes('HAZIR') || msg.includes('OK') ? '#4caf50'
                         : msg.includes('WARNING') || msg.includes('atlani') ? '#ffc107'
                         : '#888';
        line.textContent = msg;
        r.appendChild(line);
        r.scrollTop = r.scrollHeight;
      }
    };
    es.onerror = function() {
      es.close();
      btn.disabled = false;
    };
  }

  function openClip(path) {
    fetch('/api/open_clip', {method:'POST', body: JSON.stringify({clip: path}), headers:{'Content-Type':'application/json'}});
  }

  document.getElementById('btn-test-a').addEventListener('click', function() { testRecord('ankara'); });
  document.getElementById('btn-test-i').addEventListener('click', function() { testRecord('istanbul'); });
});

function selLog(k) {
  document.getElementById('log-a').style.display = k==='a'?'':'none';
  document.getElementById('log-i').style.display = k==='i'?'':'none';
  document.getElementById('ltab-a').className = 'ltab'+(k==='a'?' act-a':'');
  document.getElementById('ltab-i').className = 'ltab'+(k==='i'?' act-i':'');
}

function updateBtns(key, alive) {
  var s = document.getElementById('btn-start-'+key);
  var p = document.getElementById('btn-stop-'+key);
  var l = document.getElementById('lbl-'+key);
  if (s) s.style.display = alive ? 'none' : '';
  if (p) p.style.display = alive ? '' : 'none';
  if (l) { l.textContent = alive ? '● Çalışıyor' : 'Durdu'; l.style.color = alive ? '#4caf50' : '#444'; }
}

function setPipeUI(key, alive, ytCount, clipCount) {
  var dot = document.getElementById('dot-'+key);
  var txt = document.getElementById('stxt-'+key);
  if (dot) dot.className = 'dot ' + (alive?'on':'off');
  if (txt) { txt.className = 'status-txt '+(alive?'on':'off'); txt.textContent = alive?'Çalışıyor':'Durdu'; }
  var yt = document.getElementById('yt-'+key);
  var cl = document.getElementById('clips-'+key);
  if (yt) yt.textContent = ytCount;
  if (cl) cl.textContent = clipCount;
  updateBtns(key, alive);
}

function loadStats() {
  fetch('/api/status').then(r=>r.json()).then(d => {
    setPipeUI('a', d.daemon_ankara,  d.yt_a, d.clips_a);
    setPipeUI('i', d.daemon_istanbul, d.yt_i, d.clips_i);
    document.getElementById('next-time').textContent = d.next_time;

    // schedule pills
    const sched = document.getElementById('sched-times');
    sched.innerHTML = d.schedule.map(s =>
      `<div class="st ${s.past?'past':s.next?'next':s.active?'active':''}">${s.time}${s.next?' ← sonraki':''}</div>`
    ).join('');

    // uploads
    const ulist = document.getElementById('ulist');
    const all = [...(d.uploads_a||[]).map(u=>({...u,city:'a'})),
                 ...(d.uploads_i||[]).map(u=>({...u,city:'i'}))];
    if (all.length) {
      ulist.innerHTML = all.map(u => `
        <div class="uitem">
          <span class="utag ${u.city}">${u.city==='a'?'🚌 Ankara':'🌉 İstanbul'}</span>
          <span class="utitle">${u.title||'—'}</span>
          <a href="${u.url}" target="_blank">▶ İzle</a>
        </div>`).join('');
    } else {
      ulist.innerHTML = '<div class="empty-msg">Henüz bugün yükleme yok</div>';
    }
  });
}

function loadLogs() {
  fetch('/api/logs/ankara').then(r=>r.json()).then(d => {
    const b = document.getElementById('log-a');
    b.innerHTML = '';
    d.lines.forEach(ln => {
      const m = ln.match(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ (INFO|WARNING|ERROR)\s+(.*)$/);
      const [cls, txt] = m
        ? [m[1]==='WARNING'?'w':m[1]==='ERROR'?'e':
           /yuklendi|TAMAM|basarili/i.test(m[2])?'ok':'i', m[2]]
        : ['m', ln];
      const d2 = document.createElement('div');
      d2.className = cls; d2.textContent = txt;
      b.appendChild(d2);
    });
    b.scrollTop = b.scrollHeight;
  });
  fetch('/api/logs/istanbul').then(r=>r.json()).then(d => {
    const b = document.getElementById('log-i');
    b.innerHTML = '';
    d.lines.forEach(ln => {
      const m = ln.match(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ (INFO|WARNING|ERROR)\s+(.*)$/);
      const [cls, txt] = m
        ? [m[1]==='WARNING'?'w':m[1]==='ERROR'?'e':
           /yuklendi|TAMAM|basarili/i.test(m[2])?'ok':'i', m[2]]
        : ['m', ln];
      const d2 = document.createElement('div');
      d2.className = cls; d2.textContent = txt;
      b.appendChild(d2);
    });
    b.scrollTop = b.scrollHeight;
  });
}

function openInExplorer(path) {
  fetch('/api/open_clip', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({clip: path})
  });
}

function loadClips() {
  fetch('/api/clips').then(r=>r.json()).then(d => {
    var all = [...(d.ankara||[]).map(c=>({...c,city:'a'})),
               ...(d.istanbul||[]).map(c=>({...c,city:'i'}))];
    all.sort(function(a,b){ return b.name.localeCompare(a.name); });
    var el = document.getElementById('clips-list');
    if (!all.length) {
      el.innerHTML = '<div class="empty-msg">Henüz klip yok</div>';
      return;
    }
    el.innerHTML = all.slice(0,10).map(function(c) {
      return '<div class="uitem">' +
        '<span class="utag ' + c.city + '">' + (c.city==='a'?'🚌 Ankara':'🌉 İstanbul') + '</span>' +
        '<span class="utitle" title="' + c.path + '">' + (c.title || c.name) + '</span>' +
        '<span style="font-size:10px;color:#555;margin-right:8px">' + c.size_mb + ' MB</span>' +
        '<button onclick="openInExplorer(\'' + c.path.replace(/\\/g,'\\\\') + '\')" ' +
        'style="background:#1a1a1a;border:1px solid #333;color:#aaa;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px">📁 Aç</button>' +
        '</div>';
    }).join('');
  });
}

loadStats();
loadLogs();
loadClips();
setInterval(loadStats, 20000);
setInterval(loadLogs, 30000);
setInterval(loadClips, 30000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(TMPL)


@app.route("/api/test/record/<pipeline>")
def api_test_record(pipeline):
    if pipeline not in ("ankara", "istanbul"):
        return jsonify({"error": "bad pipeline"}), 400
    script = "main.py" if pipeline == "ankara" else "istanbul_main.py"
    clips_dir = CLIPS_A if pipeline == "ankara" else CLIPS_I

    def generate():
        import io
        proc = subprocess.Popen(
            [sys.executable, "-u", script, "--record-only", "--count", "1"],
            cwd=str(Path(__file__).parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        yield "data: BAŞLADI\n\n"
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                yield f"data: {line}\n\n"
        proc.wait()
        # Son klip
        clips = sorted(clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if clips:
            yield f"data: __CLIP__{clips[0]}\n\n"
        else:
            yield "data: __FAIL__Klip oluşturulamadı\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/open_clip", methods=["POST"])
def api_open_clip():
    import json as _json
    data = _json.loads(request.data)
    clip = data.get("clip", "")
    if clip and Path(clip).exists():
        # Dosyayı seçili olarak File Explorer'da aç
        subprocess.Popen(["explorer", "/select,", clip])
        return jsonify({"ok": True})
    elif clip:
        # Dosya yoksa klasörü aç
        folder = str(Path(clip).parent)
        subprocess.Popen(["explorer", folder])
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400

@app.route("/api/clips")
def api_clips():
    """Son kaydedilen klipleri döndür."""
    def get_clips(clips_dir, city):
        if not clips_dir.exists():
            return []
        clips = sorted(clips_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        result = []
        for c in clips:
            meta_path = c.with_suffix(".meta.json")
            title = ""
            if meta_path.exists():
                try:
                    import json as _j
                    m = _j.loads(meta_path.read_text(encoding="utf-8"))
                    title = m.get("title", "")
                except:
                    pass
            result.append({
                "path": str(c),
                "name": c.name,
                "title": title,
                "city": city,
                "size_mb": round(c.stat().st_size / 1024 / 1024, 1),
                "folder": str(c.parent),
            })
        return result
    return jsonify({
        "ankara": get_clips(CLIPS_A, "ankara"),
        "istanbul": get_clips(CLIPS_I, "istanbul"),
    })

@app.route("/api/daemon/<action>/<pipeline>", methods=["POST"])
def api_daemon(action, pipeline):
    if pipeline not in _daemons: return jsonify({"error":"bad pipeline"}), 400
    if action == "start":
        _start_daemon(pipeline)
        return jsonify({"running": True})
    elif action == "stop":
        _stop_daemon(pipeline)
        return jsonify({"running": False})
    return jsonify({"error":"bad action"}), 400


@app.route("/api/status")
def api_status():
    cfg = _cfg()
    times = cfg["schedule"]["times"]
    now = datetime.now()
    nm = now.hour * 60 + now.minute

    # schedule pills
    sched_items = []
    next_idx = None
    for i, t in enumerate(sorted(times)):
        h, m = map(int, t.split(":"))
        tm = h * 60 + m
        past   = tm < nm
        active = abs(tm - nm) <= 30
        if not past and next_idx is None:
            next_idx = i
        sched_items.append({"time": t, "past": past, "active": active, "next": (next_idx == i)})

    nt, _ = _next_run(times)

    def clip_count(d):
        return len(list(d.glob("*.mp4"))) if d.exists() else 0

    from datetime import date as _date
    today = _date.today().isoformat()

    def yt_uploads(log_path):
        if not log_path.exists(): return []
        out = []
        for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if today in ln and "UPLOADED" in ln:
                rest = ln.split("UPLOADED", 1)[1].strip()
                vid_id = rest.split("|")[0].strip()
                title  = rest.split("|")[1].strip() if "|" in rest else ""
                out.append({"url": f"https://youtube.com/watch?v={vid_id}", "title": title})
        return out

    return jsonify({
        "daemon_ankara":   _daemon_alive("ankara"),
        "daemon_istanbul": _daemon_alive("istanbul"),
        "yt_a":    len(yt_uploads(LOG_A)),
        "yt_i":    len(yt_uploads(LOG_I)),
        "clips_a": clip_count(CLIPS_A),
        "clips_i": clip_count(CLIPS_I),
        "next_time":  nt,
        "schedule":   sched_items,
        "uploads_a":  yt_uploads(LOG_A),
        "uploads_i":  yt_uploads(LOG_I),
    })


@app.route("/api/logs/<pipeline>")
def api_logs(pipeline):
    path = LOG_A if pipeline == "ankara" else LOG_I
    return jsonify({"lines": _tail(path, 100)})


if __name__ == "__main__":
    print("AsfaltTV -> http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
