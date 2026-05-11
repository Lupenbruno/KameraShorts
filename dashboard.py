"""AsfaltTV — Kontrol paneli (Ankara, İstanbul, Çorum, Konya)."""
import json, subprocess, sys, threading, time, base64, functools
from datetime import datetime, date
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template_string, send_file, request, Response

CONFIG_PATH = Path("config.yaml")

# Windows'ta CMD penceresi açılmasını engelle
_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# Dashboard şifresi
DASHBOARD_USER = "asfalt"
DASHBOARD_PASS = "Agunat77"

# Log dosyaları
LOG_A = Path("logs/pipeline.log")
LOG_I = Path("logs/istanbul_pipeline.log")
LOG_C = Path("logs/corum_pipeline.log")
LOG_K = Path("logs/konya_pipeline.log")

# Klip dizinleri
CLIPS_A = Path("data/clips")
CLIPS_I = Path("data/istanbul_clips")
CLIPS_C = Path("data/corum_clips")
CLIPS_K = Path("data/konya_clips")

app = Flask(__name__)
_daemons = {"ankara": None, "istanbul": None, "corum": None, "konya": None}

from yolo_test import yolo_bp
app.register_blueprint(yolo_bp)

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
    return Response("Giris gerekli", 401, {"WWW-Authenticate": 'Basic realm="AsfaltTV"'})


def _cfg():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

def _daemon_alive(key):
    p = _daemons[key]
    return p is not None and p.poll() is None

def _start_daemon(key):
    if _daemon_alive(key):
        return
    if key == "ankara":
        cmd = [sys.executable, "main.py", "--daemon"]
    elif key == "istanbul":
        cmd = [sys.executable, "istanbul_main.py", "--daemon"]
    else:
        # corum, konya veya gelecekteki şehirler
        cmd = [sys.executable, "city_main.py", "--city", key, "--daemon"]
    _daemons[key] = subprocess.Popen(
        cmd, cwd=str(Path(__file__).parent), **_NW
    )

def _stop_daemon(key):
    p = _daemons[key]
    if p and p.poll() is None:
        p.terminate()
        try: p.wait(timeout=5)
        except: p.kill()
    _daemons[key] = None

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


# Pipeline metadata: key → (label, emoji, log_path, clips_dir)
PIPELINES = {
    "ankara":   ("Ankara",   "🚌", LOG_A, CLIPS_A),
    "istanbul": ("İstanbul", "🌉", LOG_I, CLIPS_I),
    "corum":    ("Çorum",    "🏛️", LOG_C, CLIPS_C),
    "konya":    ("Konya",    "🕌", LOG_K, CLIPS_K),
}


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

.main{max-width:1040px;margin:0 auto;padding:28px 20px;display:flex;flex-direction:column;gap:22px}

/* başlatma kartı */
.start-card{background:#141414;border:1px solid #222;border-radius:12px;padding:24px;text-align:center}
.start-card h2{font-size:18px;font-weight:700;margin-bottom:6px;color:#fff}
.start-card p{color:#555;font-size:12px;margin-bottom:18px;line-height:1.6}

.city-btns{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
@media(max-width:700px){.city-btns{grid-template-columns:1fr 1fr}}
.city-col{display:flex;flex-direction:column;align-items:center;gap:6px}
.city-col .lbl{font-size:10px;color:#444}

.big-btn{display:inline-flex;align-items:center;gap:8px;color:#fff;
         border:none;border-radius:8px;padding:11px 20px;font-size:13px;font-weight:700;
         cursor:pointer;transition:all .2s;width:100%;justify-content:center}
.big-btn:hover{filter:brightness(1.15);transform:translateY(-1px)}
.big-btn:disabled{background:#333!important;color:#555;cursor:not-allowed;transform:none}
.stop-btn{background:#1e1e1e;border:1px solid #333;color:#888;border-radius:8px;
          padding:9px 18px;font-size:12px;font-weight:600;cursor:pointer;transition:all .2s;
          width:100%;text-align:center}
.stop-btn:hover{border-color:#555;color:#bbb}

.util-btns{display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
.util-btn{background:#1e1e1e;border:1px solid #333;color:#888;border-radius:6px;
          padding:7px 16px;font-size:11px;font-weight:600;cursor:pointer;transition:all .2s}
.util-btn:hover{border-color:#555;color:#bbb}

/* durum kartları 2x2 */
.cards{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:600px){.cards{grid-template-columns:1fr}}
.card{background:#141414;border:1px solid #222;border-radius:10px;padding:16px}
.card-head{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.card-icon{font-size:20px}
.card-title{font-size:14px;font-weight:700;color:#fff}
.card-sub{font-size:10px;color:#444;margin-top:2px}
.status-row{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.on{background:#4caf50;box-shadow:0 0 6px #4caf5055;animation:pulse 2s infinite}
.dot.off{background:#333}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.status-txt{font-size:12px;font-weight:600}
.status-txt.on{color:#4caf50}.status-txt.off{color:#555}
.stat-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px}
.stat{background:#1a1a1a;border-radius:6px;padding:7px 8px;text-align:center}
.stat .v{font-size:18px;font-weight:700;color:#fff}
.stat .k{font-size:9px;color:#444;text-transform:uppercase;letter-spacing:.5px;margin-top:1px}

/* program */
.sched-card{background:#141414;border:1px solid #222;border-radius:10px;padding:16px}
.sched-card h3{font-size:12px;font-weight:600;color:#555;text-transform:uppercase;
               letter-spacing:.5px;margin-bottom:12px}
.sched-times{display:flex;gap:6px;flex-wrap:wrap}
.st{padding:5px 12px;border-radius:20px;font-size:12px;font-weight:600;
    background:#1a1a1a;color:#444;border:1px solid #222}
.st.past{opacity:.35}
.st.active{background:rgba(76,175,80,.15);color:#4caf50;border-color:rgba(76,175,80,.3)}
.st.next{background:rgba(255,193,7,.1);color:#ffc107;border-color:rgba(255,193,7,.25)}

/* yüklenenler */
.uploads-card{background:#141414;border:1px solid #222;border-radius:10px;padding:16px}
.uploads-card h3{font-size:12px;font-weight:600;color:#555;text-transform:uppercase;
                 letter-spacing:.5px;margin-bottom:12px}
.ulist{display:flex;flex-direction:column;gap:5px}
.uitem{display:flex;align-items:center;gap:8px;padding:8px 10px;background:#1a1a1a;
       border-radius:5px;font-size:12px}
.utag{font-size:9px;padding:2px 6px;border-radius:3px;font-weight:700;flex-shrink:0}
.utag.ankara{background:rgba(74,158,255,.12);color:#4a9eff}
.utag.istanbul{background:rgba(46,207,142,.12);color:#2ecf8e}
.utag.corum{background:rgba(255,165,0,.12);color:#ffa500}
.utag.konya{background:rgba(200,100,255,.12);color:#c864ff}
.uitem .utitle{flex:1;color:#bbb;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.uitem a{color:#e63946;font-size:11px;text-decoration:none;flex-shrink:0}
.uitem a:hover{text-decoration:underline}
.empty-msg{color:#333;font-size:12px;padding:10px 0;text-align:center}

/* log */
.log-card{background:#141414;border:1px solid #222;border-radius:10px;padding:16px}
.log-card h3{font-size:12px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}
.log-tabs{display:flex;gap:2px;flex-wrap:wrap;margin-bottom:10px}
.ltab{padding:4px 12px;font-size:11px;font-weight:600;cursor:pointer;border:none;
      background:#1a1a1a;color:#444;border-radius:4px}
.ltab.act-a{background:rgba(74,158,255,.15);color:#4a9eff}
.ltab.act-i{background:rgba(46,207,142,.15);color:#2ecf8e}
.ltab.act-c{background:rgba(255,165,0,.15);color:#ffa500}
.ltab.act-k{background:rgba(200,100,255,.15);color:#c864ff}
.logbox{background:#0e0e0e;border-radius:6px;padding:10px;height:180px;overflow-y:auto;
        font-family:Consolas,monospace;font-size:11px;line-height:1.7}
.logbox .i{color:#3d6e8a}.logbox .ok{color:#3a7a45}.logbox .w{color:#8a6830}
.logbox .e{color:#8a3030}.logbox .m{color:#333}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:#2a2a2a;border-radius:2px}

/* yükleme akış tablosu */
.flow-card{background:#141414;border:1px solid #222;border-radius:10px;padding:16px}
.flow-card h3{font-size:12px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.flow-wrap{overflow-x:auto}
.flow-tbl{width:100%;border-collapse:collapse;font-size:12px}
.flow-tbl th{background:#1a1a1a;color:#555;font-size:10px;text-transform:uppercase;
             letter-spacing:.5px;padding:8px 14px;text-align:center;border-bottom:1px solid #222}
.flow-tbl th:first-child{text-align:left}
.flow-tbl td{padding:7px 14px;text-align:center;border-bottom:1px solid #191919}
.flow-tbl td:first-child{text-align:left;color:#666;font-size:11px;font-weight:600;letter-spacing:.3px}
.flow-tbl tbody tr:last-child td{border-top:1px solid #2a2a2a;border-bottom:none;
                                  font-weight:700;color:#fff;background:#1a1a1a}
.flow-tbl tbody tr:hover td{background:#161616}
.flow-tbl tr.row-active td:first-child{color:#ffc107}
.flow-tbl tr.row-past td:first-child{color:#333}
.flow-chip{display:inline-flex;align-items:center;justify-content:center;
           min-width:28px;height:20px;padding:0 6px;border-radius:4px;
           font-size:11px;font-weight:700}
.flow-chip.on-ankara{background:rgba(74,158,255,.18);color:#4a9eff}
.flow-chip.on-istanbul{background:rgba(46,207,142,.18);color:#2ecf8e}
.flow-chip.on-corum{background:rgba(255,165,0,.18);color:#ffa500}
.flow-chip.on-konya{background:rgba(200,100,255,.18);color:#c864ff}
.flow-chip.off{color:#2a2a2a}
.flow-total{font-weight:700;color:#aaa!important}
.flow-grand{font-weight:700;color:#fff!important}
</style>
</head>
<body>

<div class="header">
  <div class="logo">Asfalt<b>TV</b></div>
  <div class="spacer"></div>
  <div class="next-badge">Sonraki yayın: <b id="next-time">—</b></div>
  <a href="/yolo-test" target="_blank" style="margin-left:16px;padding:6px 16px;background:#1a1a1a;border:1px solid #00e5ff;color:#00e5ff;border-radius:6px;font-size:.85rem;text-decoration:none;">🤖 YOLO Test</a>
</div>

<div class="main">

  <!-- BAŞLATMA KARTI -->
  <div class="start-card">
    <h2>Sistemi Başlat</h2>
    <p>Her şehir günde 6 vakitte otomatik kayıt + YouTube yüklemesi yapar.</p>

    <div class="city-btns">
      <div class="city-col">
        <button class="big-btn" id="btn-start-a" style="background:#2d6fbf">▶ Ankara</button>
        <button class="stop-btn" id="btn-stop-a" style="display:none">⏹ Durdur</button>
        <span class="lbl" id="lbl-a">Durdu</span>
      </div>
      <div class="city-col">
        <button class="big-btn" id="btn-start-i" style="background:#1a7a55">▶ İstanbul</button>
        <button class="stop-btn" id="btn-stop-i" style="display:none">⏹ Durdur</button>
        <span class="lbl" id="lbl-i">Durdu</span>
      </div>
      <div class="city-col">
        <button class="big-btn" id="btn-start-c" style="background:#7a4e1a">▶ Çorum</button>
        <button class="stop-btn" id="btn-stop-c" style="display:none">⏹ Durdur</button>
        <span class="lbl" id="lbl-c">Durdu</span>
      </div>
      <div class="city-col">
        <button class="big-btn" id="btn-start-k" style="background:#5a1a7a">▶ Konya</button>
        <button class="stop-btn" id="btn-stop-k" style="display:none">⏹ Durdur</button>
        <span class="lbl" id="lbl-k">Durdu</span>
      </div>
    </div>

    <div class="util-btns">
      <button class="util-btn" id="btn-start-all">▶▶ Tümünü Başlat</button>
      <button class="util-btn" id="btn-test" style="border-color:#ffc107;color:#ffc107">⚡ Sistem Test</button>
      <button class="util-btn" id="btn-test-a" style="border-color:#4a9eff;color:#4a9eff">🚌 Ankara Test</button>
      <button class="util-btn" id="btn-test-i" style="border-color:#2ecf8e;color:#2ecf8e">🌉 İstanbul Test</button>
      <button class="util-btn" id="btn-test-c" style="border-color:#ffa500;color:#ffa500">🏛️ Çorum Test</button>
      <button class="util-btn" id="btn-test-k" style="border-color:#c864ff;color:#c864ff">🕌 Konya Test</button>
    </div>

    <div id="test-result" style="margin-top:12px;font-size:12px;color:#444;line-height:1.6"></div>
  </div>

  <!-- DURUM KARTLARI 2x2 -->
  <div class="cards">
    <div class="card">
      <div class="card-head"><div class="card-icon">🚌</div>
        <div><div class="card-title">Ankara</div>
        <div class="card-sub">EGO otobüs kameraları · 30sn · Dikey</div></div>
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
      <div class="card-head"><div class="card-icon">🌉</div>
        <div><div class="card-title">İstanbul</div>
        <div class="card-sub">Turistik kameralar · 3dk · Yatay</div></div>
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
    <div class="card">
      <div class="card-head"><div class="card-icon">🏛️</div>
        <div><div class="card-title">Çorum</div>
        <div class="card-sub">Tarihi çarşı & meydan · 3dk · Yatay</div></div>
      </div>
      <div class="status-row">
        <div class="dot off" id="dot-c"></div>
        <div class="status-txt off" id="stxt-c">Durdu</div>
      </div>
      <div class="stat-grid">
        <div class="stat"><div class="v" id="yt-c">0</div><div class="k">Bugün YT</div></div>
        <div class="stat"><div class="v" id="clips-c">0</div><div class="k">Klip</div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><div class="card-icon">🕌</div>
        <div><div class="card-title">Konya</div>
        <div class="card-sub">Mevlana & tarihi mekânlar · 3dk · Yatay</div></div>
      </div>
      <div class="status-row">
        <div class="dot off" id="dot-k"></div>
        <div class="status-txt off" id="stxt-k">Durdu</div>
      </div>
      <div class="stat-grid">
        <div class="stat"><div class="v" id="yt-k">0</div><div class="k">Bugün YT</div></div>
        <div class="stat"><div class="v" id="clips-k">0</div><div class="k">Klip</div></div>
      </div>
    </div>
  </div>

  <!-- YÜKLEME AKIŞI TABLOSU -->
  <div class="flow-card">
    <h3>Yükleme Akışı — Config</h3>
    <div class="flow-wrap"><div id="flow-table" style="color:#333;font-size:12px">Yükleniyor...</div></div>
  </div>

  <!-- PROGRAM -->
  <div class="sched-card">
    <h3>Günlük Program</h3>
    <div class="sched-times" id="sched-times"></div>
  </div>

  <!-- BUGÜN YÜKLENDİ -->
  <div class="uploads-card">
    <h3>Bugün Yüklenenler</h3>
    <div class="ulist" id="ulist"><div class="empty-msg">Henüz yükleme yok</div></div>
  </div>

  <!-- SON KLİPLER -->
  <div class="uploads-card">
    <h3>Son Kaydedilen Klipler</h3>
    <div class="ulist" id="clips-list"><div class="empty-msg">Klip bulunamadı</div></div>
  </div>

  <!-- LOG -->
  <div class="log-card">
    <h3>Log</h3>
    <div class="log-tabs">
      <button class="ltab act-a" id="ltab-a" onclick="selLog('a')">🚌 Ankara</button>
      <button class="ltab"        id="ltab-i" onclick="selLog('i')">🌉 İstanbul</button>
      <button class="ltab"        id="ltab-c" onclick="selLog('c')">🏛️ Çorum</button>
      <button class="ltab"        id="ltab-k" onclick="selLog('k')">🕌 Konya</button>
    </div>
    <div class="logbox" id="log-a"></div>
    <div class="logbox" id="log-i" style="display:none"></div>
    <div class="logbox" id="log-c" style="display:none"></div>
    <div class="logbox" id="log-k" style="display:none"></div>
  </div>

</div>

<script>
const CITY_LABELS = {a:'🚌 Ankara',i:'🌉 İstanbul',c:'🏛️ Çorum',k:'🕌 Konya'};
const CITY_KEYS   = {a:'ankara',   i:'istanbul',   c:'corum',   k:'konya'};
const TEST_LABELS = {a:'🚌 Ankara Test',i:'🌉 İstanbul Test',c:'🏛️ Çorum Test',k:'🕌 Konya Test'};

document.addEventListener('DOMContentLoaded', function() {

  // Başlat / Durdur butonları
  ['a','i','c','k'].forEach(function(k) {
    document.getElementById('btn-start-'+k).addEventListener('click', function() {
      fetch('/api/daemon/start/'+CITY_KEYS[k], {method:'POST'})
        .then(function() { setTimeout(loadStats, 1000); });
    });
    document.getElementById('btn-stop-'+k).addEventListener('click', function() {
      if (!confirm(CITY_LABELS[k]+' durdurulsun mu?')) return;
      fetch('/api/daemon/stop/'+CITY_KEYS[k], {method:'POST'})
        .then(function() { setTimeout(loadStats, 1000); });
    });
  });

  // Tümünü başlat
  document.getElementById('btn-start-all').addEventListener('click', function() {
    ['a','i','c','k'].forEach(function(k) {
      fetch('/api/daemon/start/'+CITY_KEYS[k], {method:'POST'});
    });
    setTimeout(loadStats, 1200);
  });

  // Sistem test
  document.getElementById('btn-test').addEventListener('click', function() {
    var r = document.getElementById('test-result');
    r.style.color = '#ffc107'; r.textContent = 'API test ediliyor...';
    fetch('/api/status').then(function(res){return res.json();}).then(function(d){
      r.style.color = '#4caf50';
      r.textContent = 'SISTEM OK — Ankara:'+(d.daemon_ankara?'✅':'⏸')
        +' İstanbul:'+(d.daemon_istanbul?'✅':'⏸')
        +' Çorum:'+(d.daemon_corum?'✅':'⏸')
        +' Konya:'+(d.daemon_konya?'✅':'⏸');
    }).catch(function(e){
      r.style.color='#e63946'; r.textContent='HATA: '+e;
    });
  });

  // Test kayıt butonları
  function testRecord(pipe, shortKey) {
    var r = document.getElementById('test-result');
    var btn = document.getElementById('btn-test-'+shortKey);
    btn.disabled = true;
    btn.textContent = '⏳ Kayıt alınıyor...';
    r.style.cssText = 'margin-top:12px;font-size:11px;color:#aaa;line-height:1.8;'
      + 'text-align:left;background:#0e0e0e;padding:10px;border-radius:6px;'
      + 'max-height:220px;overflow-y:auto;font-family:Consolas,monospace';
    r.textContent = '';
    var es = new EventSource('/api/test/record/'+pipe);
    es.onmessage = function(e) {
      var msg = e.data;
      if (msg.startsWith('__CLIP__')) {
        es.close(); btn.disabled = false; btn.textContent = TEST_LABELS[shortKey];
        var clipPath = msg.replace('__CLIP__','');
        var fname = clipPath.split('\\').pop();
        var div = document.createElement('div');
        div.style.cssText='margin-top:8px;padding:8px;background:#1a2e1a;border-radius:4px;color:#4caf50;font-family:sans-serif';
        div.innerHTML = '✅ <b>BAŞARILI:</b> '+fname+' &nbsp;'
          +'<button onclick="openInExplorer(\''+clipPath.replace(/\\/g,'\\\\')+'\') " '
          +'style="background:#4caf50;color:#fff;border:none;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px">📁 Klasörde Aç</button>';
        r.appendChild(div); r.scrollTop=r.scrollHeight; loadClips();
      } else if (msg.startsWith('__FAIL__')) {
        es.close(); btn.disabled = false; btn.textContent = TEST_LABELS[shortKey];
        var div = document.createElement('div');
        div.style.color='#e63946'; div.textContent='❌ '+msg.replace('__FAIL__','');
        r.appendChild(div);
      } else {
        var line = document.createElement('div');
        line.style.color = msg.includes('HATA')||msg.includes('hata')?'#e63946'
          : msg.includes('HAZIR')||msg.includes('OK')?'#4caf50'
          : msg.includes('WARNING')||msg.includes('atlani')?'#ffc107':'#888';
        line.textContent = msg;
        r.appendChild(line); r.scrollTop=r.scrollHeight;
      }
    };
    es.onerror = function(){ es.close(); btn.disabled=false; btn.textContent=TEST_LABELS[shortKey]; };
  }

  document.getElementById('btn-test-a').addEventListener('click', function(){ testRecord('ankara',  'a'); });
  document.getElementById('btn-test-i').addEventListener('click', function(){ testRecord('istanbul','i'); });
  document.getElementById('btn-test-c').addEventListener('click', function(){ testRecord('corum',   'c'); });
  document.getElementById('btn-test-k').addEventListener('click', function(){ testRecord('konya',   'k'); });
});

// Log sekme seçici
function selLog(k) {
  ['a','i','c','k'].forEach(function(x){
    document.getElementById('log-'+x).style.display = x===k?'':'none';
    document.getElementById('ltab-'+x).className = 'ltab'+(x===k?' act-'+x:'');
  });
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
  var dot  = document.getElementById('dot-'+key);
  var txt  = document.getElementById('stxt-'+key);
  if (dot) dot.className = 'dot '+(alive?'on':'off');
  if (txt) { txt.className = 'status-txt '+(alive?'on':'off'); txt.textContent = alive?'Çalışıyor':'Durdu'; }
  var yt = document.getElementById('yt-'+key);
  var cl = document.getElementById('clips-'+key);
  if (yt) yt.textContent = ytCount;
  if (cl) cl.textContent = clipCount;
  updateBtns(key, alive);
}

function loadStats() {
  fetch('/api/status').then(r=>r.json()).then(d => {
    setPipeUI('a', d.daemon_ankara,   d.yt_a, d.clips_a);
    setPipeUI('i', d.daemon_istanbul, d.yt_i, d.clips_i);
    setPipeUI('c', d.daemon_corum,    d.yt_c, d.clips_c);
    setPipeUI('k', d.daemon_konya,    d.yt_k, d.clips_k);
    document.getElementById('next-time').textContent = d.next_time;

    const sched = document.getElementById('sched-times');
    sched.innerHTML = d.schedule.map(s =>
      `<div class="st ${s.past?'past':s.next?'next':s.active?'active':''}">${s.time}${s.next?' ← sonraki':''}</div>`
    ).join('');

    const ulist = document.getElementById('ulist');
    const all = [
      ...(d.uploads_a||[]).map(u=>({...u,city:'ankara'})),
      ...(d.uploads_i||[]).map(u=>({...u,city:'istanbul'})),
      ...(d.uploads_c||[]).map(u=>({...u,city:'corum'})),
      ...(d.uploads_k||[]).map(u=>({...u,city:'konya'})),
    ];
    if (all.length) {
      const EMOJI = {ankara:'🚌',istanbul:'🌉',corum:'🏛️',konya:'🕌'};
      ulist.innerHTML = all.map(u => `
        <div class="uitem">
          <span class="utag ${u.city}">${EMOJI[u.city]||''} ${u.city}</span>
          <span class="utitle">${u.title||'—'}</span>
          <a href="${u.url}" target="_blank">▶ İzle</a>
        </div>`).join('');
    } else {
      ulist.innerHTML = '<div class="empty-msg">Henüz bugün yükleme yok</div>';
    }
  });
}

function loadLogs() {
  const LOGKEYS = {a:'ankara', i:'istanbul', c:'corum', k:'konya'};
  Object.entries(LOGKEYS).forEach(([k, pipe]) => {
    fetch('/api/logs/'+pipe).then(r=>r.json()).then(d => {
      const b = document.getElementById('log-'+k);
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
  });
}

function openInExplorer(path) {
  fetch('/api/open_clip', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({clip: path})
  });
}

function loadClips() {
  fetch('/api/clips').then(r=>r.json()).then(d => {
    var all = [
      ...(d.ankara  ||[]).map(c=>({...c,city:'ankara'})),
      ...(d.istanbul||[]).map(c=>({...c,city:'istanbul'})),
      ...(d.corum   ||[]).map(c=>({...c,city:'corum'})),
      ...(d.konya   ||[]).map(c=>({...c,city:'konya'})),
    ];
    all.sort(function(a,b){ return b.name.localeCompare(a.name); });
    var el = document.getElementById('clips-list');
    if (!all.length) { el.innerHTML='<div class="empty-msg">Henüz klip yok</div>'; return; }
    const EMOJI = {ankara:'🚌',istanbul:'🌉',corum:'🏛️',konya:'🕌'};
    el.innerHTML = all.slice(0,12).map(function(c) {
      return '<div class="uitem">'
        +'<span class="utag '+c.city+'">'+(EMOJI[c.city]||'')+' '+c.city+'</span>'
        +'<span class="utitle" title="'+c.path+'">'+(c.title||c.name)+'</span>'
        +'<span style="font-size:10px;color:#555;margin-right:6px">'+c.size_mb+' MB</span>'
        +'<button onclick="openInExplorer(\''+c.path.replace(/\\/g,'\\\\')+'\') " '
        +'style="background:#1a1a1a;border:1px solid #333;color:#aaa;padding:2px 8px;'
        +'border-radius:4px;cursor:pointer;font-size:11px">📁 Aç</button>'
        +'</div>';
    }).join('');
  });
}

function loadFlowTable() {
  fetch('/api/schedule_table').then(r=>r.json()).then(d => {
    const now = new Date();
    const nm  = now.getHours()*60 + now.getMinutes();
    const CITIES  = ['ankara','istanbul','corum','konya'];
    const EMOJI   = {ankara:'🚌',istanbul:'🌉',corum:'🏛️',konya:'🕌'};
    const LABELS  = {ankara:'Ankara',istanbul:'İstanbul',corum:'Çorum',konya:'Konya'};

    let h = '<table class="flow-tbl"><thead><tr><th>Saat</th>';
    CITIES.forEach(c => { h += `<th>${EMOJI[c]} ${LABELS[c]}</th>`; });
    h += '<th>Toplam</th></tr></thead><tbody>';

    d.rows.forEach(row => {
      const [hh,mm] = row.time.split(':').map(Number);
      const tm = hh*60 + mm;
      const isActive = Math.abs(tm - nm) <= 30;
      const isPast   = tm < nm - 30;
      const cls = isActive ? 'row-active' : isPast ? 'row-past' : '';
      h += `<tr class="${cls}">`;
      h += `<td>${row.time}${isActive ? ' ◀' : ''}</td>`;
      CITIES.forEach(c => {
        const v = row.cities[c] || 0;
        h += v
          ? `<td><span class="flow-chip on-${c}">${v}</span></td>`
          : `<td><span class="flow-chip off">—</span></td>`;
      });
      h += `<td class="flow-total">${row.total}</td></tr>`;
    });

    h += '<tr><td>TOPLAM</td>';
    CITIES.forEach(c => { h += `<td class="flow-grand">${d.city_totals[c]}</td>`; });
    h += `<td class="flow-grand">${d.grand_total} / gün</td></tr>`;
    h += '</tbody></table>';

    document.getElementById('flow-table').innerHTML = h;
  }).catch(()=>{});
}

loadStats(); loadLogs(); loadClips(); loadFlowTable();
setInterval(loadStats,     20000);
setInterval(loadLogs,      30000);
setInterval(loadClips,     30000);
setInterval(loadFlowTable, 60000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(TMPL)


@app.route("/api/test/record/<pipeline>")
def api_test_record(pipeline):
    if pipeline not in ("ankara", "istanbul", "corum", "konya"):
        return jsonify({"error": "bad pipeline"}), 400

    if pipeline == "ankara":
        cmd = [sys.executable, "-u", "main.py", "--record-only", "--count", "1"]
        clips_dir = CLIPS_A
    elif pipeline == "istanbul":
        cmd = [sys.executable, "-u", "istanbul_main.py", "--record-only", "--count", "1"]
        clips_dir = CLIPS_I
    elif pipeline == "corum":
        cmd = [sys.executable, "-u", "city_main.py", "--city", "corum", "--record-only", "--count", "1"]
        clips_dir = CLIPS_C
    else:  # konya
        cmd = [sys.executable, "-u", "city_main.py", "--city", "konya", "--record-only", "--count", "1"]
        clips_dir = CLIPS_K

    def generate():
        proc = subprocess.Popen(
            cmd,
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
        clips = sorted(clips_dir.glob("*.mp4"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if clips:
            yield f"data: __CLIP__{clips[0]}\n\n"
        else:
            yield "data: __FAIL__Klip oluşturulamadı\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/open_clip", methods=["POST"])
def api_open_clip():
    data = json.loads(request.data)
    clip = data.get("clip", "")
    if clip and Path(clip).exists():
        subprocess.Popen(["explorer", "/select,", clip])
        return jsonify({"ok": True})
    elif clip:
        folder = str(Path(clip).parent)
        subprocess.Popen(["explorer", folder])
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/api/clips")
def api_clips():
    def get_clips(clips_dir, city):
        if not clips_dir.exists():
            return []
        clips = sorted(clips_dir.glob("*.mp4"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        result = []
        for c in clips:
            meta_path = c.with_suffix(".meta.json")
            title = ""
            if meta_path.exists():
                try:
                    m = json.loads(meta_path.read_text(encoding="utf-8"))
                    title = m.get("title", "")
                except:
                    pass
            result.append({
                "path": str(c), "name": c.name, "title": title, "city": city,
                "size_mb": round(c.stat().st_size / 1024 / 1024, 1),
            })
        return result

    return jsonify({
        "ankara":   get_clips(CLIPS_A, "ankara"),
        "istanbul": get_clips(CLIPS_I, "istanbul"),
        "corum":    get_clips(CLIPS_C, "corum"),
        "konya":    get_clips(CLIPS_K, "konya"),
    })


@app.route("/api/daemon/<action>/<pipeline>", methods=["POST"])
def api_daemon(action, pipeline):
    if pipeline not in _daemons:
        return jsonify({"error": "bad pipeline"}), 400
    if action == "start":
        _start_daemon(pipeline)
        return jsonify({"running": True})
    elif action == "stop":
        _stop_daemon(pipeline)
        return jsonify({"running": False})
    return jsonify({"error": "bad action"}), 400


@app.route("/api/status")
def api_status():
    cfg = _cfg()
    times = cfg["schedule"]["times"]
    now = datetime.now()
    nm = now.hour * 60 + now.minute

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

    today = date.today().isoformat()

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
        "daemon_corum":    _daemon_alive("corum"),
        "daemon_konya":    _daemon_alive("konya"),
        "yt_a":    len(yt_uploads(LOG_A)),
        "yt_i":    len(yt_uploads(LOG_I)),
        "yt_c":    len(yt_uploads(LOG_C)),
        "yt_k":    len(yt_uploads(LOG_K)),
        "clips_a": clip_count(CLIPS_A),
        "clips_i": clip_count(CLIPS_I),
        "clips_c": clip_count(CLIPS_C),
        "clips_k": clip_count(CLIPS_K),
        "next_time":  nt,
        "schedule":   sched_items,
        "uploads_a":  yt_uploads(LOG_A),
        "uploads_i":  yt_uploads(LOG_I),
        "uploads_c":  yt_uploads(LOG_C),
        "uploads_k":  yt_uploads(LOG_K),
    })


@app.route("/api/schedule_table")
def api_schedule_table():
    cfg = _cfg()
    pipelines = {
        "ankara":   (cfg["schedule"]["times"],               cfg["schedule"].get("videos_per_slot", 1)),
        "istanbul": (cfg["istanbul"]["times"],                cfg["istanbul"].get("videos_per_slot", 1)),
        "corum":    (cfg["cities"]["corum"]["times"],         cfg["cities"]["corum"].get("videos_per_slot", 1)),
        "konya":    (cfg["cities"]["konya"]["times"],         cfg["cities"]["konya"].get("videos_per_slot", 1)),
    }
    all_times = sorted({t for times, _ in pipelines.values() for t in times})
    rows = []
    for t in all_times:
        row = {"time": t, "cities": {}, "total": 0}
        for city, (times, vps) in pipelines.items():
            v = vps if t in times else 0
            row["cities"][city] = v
            row["total"] += v
        rows.append(row)
    city_totals = {city: len(times) * vps for city, (times, vps) in pipelines.items()}
    return jsonify({
        "rows": rows,
        "city_totals": city_totals,
        "grand_total": sum(city_totals.values()),
    })


@app.route("/api/logs/<pipeline>")
def api_logs(pipeline):
    log_map = {
        "ankara":   LOG_A,
        "istanbul": LOG_I,
        "corum":    LOG_C,
        "konya":    LOG_K,
    }
    path = log_map.get(pipeline, LOG_A)
    return jsonify({"lines": _tail(path, 100)})


if __name__ == "__main__":
    # Telegram bot başlat
    try:
        cfg = _cfg()
        tg = cfg.get("telegram", {})
        token = tg.get("bot_token", "")
        chat_id = tg.get("chat_id", "")
        if token and chat_id:
            from src.telegram_notifier import init_notifier
            notifier = init_notifier(
                token=token,
                chat_id=chat_id,
                log_paths={
                    "ankara":   LOG_A,
                    "istanbul": LOG_I,
                    "corum":    LOG_C,
                    "konya":    LOG_K,
                },
                pipelines_ref=_daemons,
                start_fn=_start_daemon,
                stop_fn=_stop_daemon,
            )
            notifier.start()
            notifier.notify_start()
            print("Telegram bot aktif")
    except Exception as e:
        print(f"Telegram başlatılamadı: {e}")

    print("AsfaltTV -> http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
