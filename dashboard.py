"""AsfaltTV Dashboard — Kaydet / Incele / Yukle"""
import json
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, date
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, render_template_string, request, send_file

CONFIG_PATH = Path("config.yaml")
CLIPS_DIR   = Path("data/clips")
IST_DIR     = Path("data/istanbul_clips")
LOG_PATH    = Path("logs/pipeline.log")
IST_LOG     = Path("logs/istanbul_pipeline.log")

app = Flask(__name__)

_rec_q   = {"ankara": queue.Queue(maxsize=500), "istanbul": queue.Queue(maxsize=500)}
_rec_run = {"ankara": threading.Event(),         "istanbul": threading.Event()}
_daemon  = {"ankara": None, "istanbul": None}   # subprocess.Popen objects
_upl_q   = queue.Queue(maxsize=200)
_upl_run = threading.Event()


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

def _tail(path: Path, n=200):
    if not path.exists(): return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]

def _yt_today(log_path: Path) -> int:
    if not log_path.exists(): return 0
    today = date.today().isoformat()
    return sum(1 for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
               if today in ln and "UPLOADED" in ln)

def _clips_with_meta(d: Path) -> list[dict]:
    if not d.exists(): return []
    out = []
    for f in sorted(d.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[:60]:
        meta_path = f.with_suffix(".meta.json")
        meta = {}
        if meta_path.exists():
            try: meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception: pass
        st = f.stat()
        out.append({
            "name":        f.name,
            "size_mb":     round(st.st_size / 1_048_576, 1),
            "modified":    datetime.fromtimestamp(st.st_mtime).strftime("%d/%m %H:%M"),
            "title":       meta.get("title", f.stem),
            "description": meta.get("description", ""),
            "city":        meta.get("city", "ankara"),
            "uploaded":    meta.get("uploaded", False),
            "youtube_url": meta.get("youtube_url"),
            "has_meta":    meta_path.exists(),
        })
    return out

def _ankara_cam_count():
    try:
        import requests as rq
        s = rq.Session(); s.headers["User-Agent"] = "KameraShorts/1.0"
        data = s.get("https://seyret.ankara.bel.tr/status.json", timeout=8).json()
        return sum(1 for v in data if v.get("stream_url") and v.get("is_visible"))
    except Exception: return -1

def _do_record(pipeline: str, count: int):
    script = "main.py" if pipeline == "ankara" else "istanbul_main.py"
    q = _rec_q[pipeline]
    _rec_run[pipeline].set()
    cmd = [sys.executable, script, "--record-only", f"--count={count}"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                cwd=str(Path(__file__).parent))
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                try: q.put_nowait(line)
                except queue.Full: pass
        proc.wait()
    finally:
        _rec_run[pipeline].clear()
        try: q.put_nowait("__DONE__")
        except queue.Full: pass

def _do_upload(clips: list[dict]):
    """clips: [{city, name}, ...]"""
    _upl_run.set()
    cfg = _cfg()
    try:
        from src.youtube_uploader import YouTubeUploader
        for item in clips:
            city = item["city"]
            name = item["name"]
            base = CLIPS_DIR if city == "ankara" else IST_DIR
            clip_path = base / name
            meta_path = clip_path.with_suffix(".meta.json")

            if not clip_path.exists():
                try: _upl_q.put_nowait(f"__ERR__ {name}: dosya bulunamadi")
                except queue.Full: pass
                continue

            if not meta_path.exists():
                try: _upl_q.put_nowait(f"__ERR__ {name}: meta.json yok, once kayit modunda calistir")
                except queue.Full: pass
                continue

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("uploaded"):
                try: _upl_q.put_nowait(f"__SKIP__ {name}: zaten yuklendi → {meta.get('youtube_url')}")
                except queue.Full: pass
                continue

            # Uploader olustur
            if city == "istanbul":
                uc = dict(cfg)
                uc["youtube"] = cfg.get("istanbul_youtube") or cfg["youtube"]
                uc["paths"] = dict(cfg["paths"])
                uc["paths"]["log_path"]  = cfg["paths"].get("istanbul_log_path", "logs/istanbul_pipeline.log")
                uc["paths"]["queue_path"]= cfg["paths"].get("istanbul_queue_path", "data/queue/istanbul_upload_queue.json")
            else:
                uc = cfg

            try:
                uploader = YouTubeUploader(uc)
                try: _upl_q.put_nowait(f"__INFO__ {name}: yukleniyor...")
                except queue.Full: pass

                result = uploader.upload(str(clip_path), meta)
                meta["uploaded"]    = True
                meta["youtube_url"] = result["url"]
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                try: _upl_q.put_nowait(f"__OK__ {name}: {result['url']}")
                except queue.Full: pass
            except Exception as e:
                try: _upl_q.put_nowait(f"__ERR__ {name}: {e}")
                except queue.Full: pass
    finally:
        _upl_run.clear()
        try: _upl_q.put_nowait("__DONE__")
        except queue.Full: pass

def _next_run(times):
    now = datetime.now()
    now_mins = now.hour * 60 + now.minute
    for t in sorted(times):
        h, m = map(int, t.split(":"))
        if h * 60 + m > now_mins:
            delta = (h * 60 + m - now_mins)
            return f"{t} ({delta}dk sonra)"
    return f"{sorted(times)[0]} (yarin)"


# ── HTML ─────────────────────────────────────────────────────────────────────

TMPL = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AsfaltTV</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0e0e0e;--bg2:#141414;--bg3:#1a1a1a;--bg4:#222;
  --b:#222;--b2:#2a2a2a;
  --txt:#d0d0d0;--txt2:#888;--txt3:#444;
  --anka:#4a9eff;--ist:#2ecf8e;
  --red:#e63946;--yel:#ffc107;--grn:#4caf50;
}
body{background:var(--bg);color:var(--txt);font-family:'Segoe UI',system-ui,sans-serif;
     height:100vh;display:flex;flex-direction:column;overflow:hidden;font-size:13px}

/* topbar */
.top{background:var(--bg2);border-bottom:1px solid var(--b);height:50px;
     padding:0 18px;display:flex;align-items:center;gap:12px;flex-shrink:0}
.logo{font-size:16px;font-weight:800;color:#fff}.logo b{color:var(--red)}
.div{width:1px;height:18px;background:var(--b2);flex-shrink:0}
.spacer{flex:1}

/* quota bar */
.quota-wrap{display:flex;align-items:center;gap:7px}
.quota-lbl{font-size:10px;color:var(--txt3)}
.qtrack{width:70px;height:6px;background:var(--b2);border-radius:3px;overflow:hidden}
.qfill{height:100%;border-radius:3px;background:var(--grn);transition:width .4s}
.qfill.warn{background:var(--yel)}.qfill.full{background:var(--red)}
.quota-num{font-size:11px;font-weight:700;color:var(--txt2)}

/* schedule next */
.next-run{font-size:11px;color:var(--txt3)}
.next-run b{color:var(--yel)}

/* body */
.body{display:flex;flex:1;min-height:0}

/* left sidebar */
.sidebar{width:230px;flex-shrink:0;border-right:1px solid var(--b);
         display:flex;flex-direction:column;overflow-y:auto}

/* pipeline card */
.pc{border-bottom:1px solid var(--b);padding:14px}
.pc-head{display:flex;align-items:center;gap:7px;margin-bottom:10px}
.pc-icon{font-size:16px;line-height:1}
.pc-title{font-size:13px;font-weight:700;color:#fff}
.pc-sub{font-size:10px;color:var(--txt3);margin-top:1px}
.pc-row{display:flex;align-items:center;gap:7px;margin-bottom:8px}
.pc-label{font-size:10px;color:var(--txt3);min-width:55px}
.cnt-in{width:52px;background:var(--bg3);border:1px solid var(--b2);color:var(--txt);
        padding:4px 6px;border-radius:5px;font-size:12px;text-align:center}
.rec-btn{flex:1;border:none;border-radius:5px;padding:6px 10px;font-size:12px;font-weight:700;
         cursor:pointer;color:#fff;transition:all .2s}
.rec-btn.anka{background:rgba(74,158,255,.2);color:var(--anka);border:1px solid rgba(74,158,255,.3)}
.rec-btn.anka:hover:not(:disabled){background:var(--anka);color:#fff}
.rec-btn.ist{background:rgba(46,207,142,.15);color:var(--ist);border:1px solid rgba(46,207,142,.25)}
.rec-btn.ist:hover:not(:disabled){background:var(--ist);color:#fff}
.rec-btn:disabled{opacity:.35;cursor:not-allowed}
.rec-busy{font-size:10px;color:var(--txt3);display:flex;align-items:center;gap:5px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--b2)}
.dot.spin{background:var(--red);animation:bl .9s infinite}
.dot.ok{background:var(--grn)}
@keyframes bl{0%,100%{opacity:1}50%{opacity:.1}}

/* daemon row */
.daemon-row{display:flex;align-items:center;justify-content:space-between;margin-top:2px}
.daemon-lbl{font-size:10px;color:var(--txt3)}
.dtoggle{font-size:10px;padding:2px 8px;border-radius:4px;cursor:pointer;border:none;font-weight:600}
.dtoggle.off{background:var(--bg4);color:var(--txt3)}
.dtoggle.off:hover{color:var(--txt)}
.dtoggle.on{background:rgba(76,175,80,.2);color:var(--grn);border:1px solid rgba(76,175,80,.3)}
.dtoggle.on:hover{background:rgba(230,57,70,.2);color:var(--red)}

/* summary */
.summary{margin-top:8px;font-size:10px;padding:6px 8px;border-radius:5px;
         background:var(--bg3);color:var(--txt2);border-left:2px solid var(--b2);line-height:1.5}
.summary.ok{border-color:var(--grn);color:#5a9c5e}
.summary.warn{border-color:var(--yel);color:#a07a30}

/* center: clip list */
.cliparea{flex:1;display:flex;flex-direction:column;min-width:0}
.clip-toolbar{padding:9px 14px;border-bottom:1px solid var(--b);display:flex;align-items:center;
              gap:8px;flex-shrink:0}
.clip-filter{display:flex;gap:1px;background:var(--b);border-radius:5px;overflow:hidden}
.cf{padding:4px 12px;font-size:11px;font-weight:600;cursor:pointer;background:var(--bg3);
    color:var(--txt3);border:none;transition:all .15s}
.cf.ca{background:rgba(74,158,255,.15);color:var(--anka)}
.cf.ci{background:rgba(46,207,142,.15);color:var(--ist)}
.cf:hover:not(.ca):not(.ci){color:var(--txt);background:var(--bg4)}
.tb-spacer{flex:1}
.sel-actions{display:flex;align-items:center;gap:7px}
.sel-lbl{font-size:11px;color:var(--txt3)}
.act-btn{border:1px solid var(--b2);border-radius:5px;padding:5px 11px;font-size:11px;
         font-weight:600;cursor:pointer;background:var(--bg3);color:var(--txt2);transition:all .2s}
.act-btn:hover{color:var(--txt);border-color:#333}
.act-btn.del:hover{color:var(--red);border-color:rgba(230,57,70,.4)}
.upl-btn{background:var(--red);color:#fff;border:none;border-radius:5px;padding:6px 14px;
         font-size:12px;font-weight:700;cursor:pointer;transition:all .2s;white-space:nowrap}
.upl-btn:hover:not(:disabled){background:#c8303c}
.upl-btn:disabled{opacity:.35;cursor:not-allowed}

/* clip list */
.clist{flex:1;overflow-y:auto}
.empty{color:var(--txt3);font-size:12px;padding:30px;text-align:center}
.crow{padding:9px 14px;border-bottom:1px solid #181818;display:flex;align-items:center;gap:9px}
.crow:hover{background:var(--bg3)}
.crow.uploaded{opacity:.45}
.crow input[type=checkbox]{width:15px;height:15px;accent-color:var(--red);cursor:pointer;flex-shrink:0}
.ctag{font-size:9px;padding:2px 6px;border-radius:3px;font-weight:700;flex-shrink:0}
.ctag.a{background:rgba(74,158,255,.12);color:var(--anka)}
.ctag.i{background:rgba(46,207,142,.12);color:var(--ist)}
.cinfo{flex:1;min-width:0;cursor:pointer}
.cn{font-size:12px;color:#ccc;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cm{font-size:10px;color:var(--txt3);margin-top:2px;display:flex;gap:8px;flex-wrap:wrap}
.ctitle{font-size:10px;color:var(--txt3);margin-top:3px;white-space:nowrap;overflow:hidden;
        text-overflow:ellipsis;font-style:italic}
.yt-link{font-size:9px;padding:1px 6px;border-radius:3px;background:rgba(230,57,70,.15);
         color:var(--red);text-decoration:none;flex-shrink:0}
.yt-link:hover{background:rgba(230,57,70,.3)}
.ib{background:none;border:1px solid var(--b2);border-radius:4px;color:var(--txt3);
    padding:3px 8px;font-size:11px;cursor:pointer;line-height:1;flex-shrink:0}
.ib:hover{border-color:#333;color:var(--txt)}

/* log panel */
.logpanel{border-top:1px solid var(--b);flex-shrink:0;display:flex;flex-direction:column}
.logtabs{display:flex;gap:0;border-bottom:1px solid var(--b);flex-shrink:0}
.ltab{padding:5px 14px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
      cursor:pointer;border:none;background:var(--bg2);color:var(--txt3);
      border-right:1px solid var(--b);border-bottom:2px solid transparent;transition:all .15s}
.ltab.la{color:var(--anka);border-bottom-color:var(--anka)}
.ltab.li{color:var(--ist);border-bottom-color:var(--ist)}
.ltab.lu{color:var(--red);border-bottom-color:var(--red)}
.log-spacer{flex:1;background:var(--bg2);border-bottom:1px solid var(--b)}
.log-clr{background:none;border:none;color:var(--txt3);font-size:10px;cursor:pointer;padding:5px 10px}
.log-clr:hover{color:var(--txt)}
.logbody{height:130px;overflow-y:auto;padding:7px 14px;font-family:Consolas,monospace;font-size:11px;line-height:1.7}
.logbody .i{color:#3d6e8a}.logbody .ok{color:#3a7a45}.logbody .w{color:#8a6830}
.logbody .e{color:#8a3030}.logbody .m{color:#555}.logbody .up{color:var(--red)}

/* upload confirm modal */
.ov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:99;align-items:center;justify-content:center}
.ov.open{display:flex}
.modal{background:#141414;border:1px solid var(--b2);border-radius:10px;padding:20px;
       width:min(460px,95vw);max-height:80vh;display:flex;flex-direction:column}
.modal-head{font-size:14px;font-weight:700;margin-bottom:14px;color:#fff;display:flex;justify-content:space-between}
.modal-list{flex:1;overflow-y:auto;margin-bottom:14px}
.modal-item{padding:10px;border-radius:6px;background:var(--bg3);margin-bottom:7px;font-size:11px}
.modal-item .mt{font-weight:600;color:#ccc;margin-bottom:4px}
.modal-item .ms{color:var(--txt3);display:flex;gap:10px}
.modal-warn{font-size:11px;color:var(--yel);margin-bottom:12px;padding:8px;
            background:rgba(255,193,7,.07);border-radius:5px;border-left:2px solid var(--yel)}
.modal-foot{display:flex;gap:8px;justify-content:flex-end}
.mbtn{border-radius:5px;padding:7px 18px;font-size:12px;font-weight:700;cursor:pointer;border:none}
.mbtn.cancel{background:var(--bg4);color:var(--txt2)}.mbtn.cancel:hover{color:var(--txt)}
.mbtn.confirm{background:var(--red);color:#fff}.mbtn.confirm:hover{background:#c8303c}
.xbtn{background:none;border:none;color:var(--txt3);font-size:16px;cursor:pointer}
.xbtn:hover{color:#ccc}

/* video modal */
.vov{display:none;position:fixed;inset:0;background:rgba(0,0,0,.93);z-index:98;align-items:center;justify-content:center}
.vov.open{display:flex}
.vbox{background:#141414;border:1px solid var(--b2);border-radius:10px;padding:12px;
      width:min(480px,95vw);position:relative}
.vbox video{width:100%;border-radius:6px;background:#000;display:block;max-height:72vh}
.vname{font-size:10px;color:var(--txt3);margin-bottom:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* scrollbars */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--b2);border-radius:2px}
</style>
</head>
<body>

<!-- TOPBAR -->
<div class="top">
  <div class="logo">Asfalt<b>TV</b></div>
  <div class="div"></div>
  <div class="quota-wrap">
    <div class="quota-lbl">YouTube Kota</div>
    <div class="qtrack"><div class="qfill" id="qfill" style="width:0"></div></div>
    <div class="quota-num" id="quota-num">0/6</div>
  </div>
  <div class="div"></div>
  <div class="next-run">Sonraki: <b id="next-run">—</b></div>
  <div class="spacer"></div>
  <span style="font-size:11px;color:var(--txt3)" id="top-status"></span>
</div>

<!-- BODY -->
<div class="body">

  <!-- SIDEBAR -->
  <div class="sidebar">

    <!-- Ankara card -->
    <div class="pc">
      <div class="pc-head">
        <div class="pc-icon">🚌</div>
        <div>
          <div class="pc-title">Ankara</div>
          <div class="pc-sub">30 sn · 9:16 Dikey · Shorts</div>
        </div>
      </div>
      <div class="pc-row">
        <div class="pc-label">Klip sayısı</div>
        <input class="cnt-in" type="number" id="cnt-a" value="3" min="1" max="20">
      </div>
      <div class="pc-row">
        <button class="rec-btn anka" id="rbtn-a" onclick="record('ankara')">▶ Kaydet</button>
        <div class="rec-busy"><div class="dot" id="dot-a"></div><span id="st-a">Hazır</span></div>
      </div>
      <div class="daemon-row">
        <div class="daemon-lbl">Otomatik (daemon)</div>
        <button class="dtoggle off" id="dtoggle-a" onclick="toggleDaemon('ankara')">Başlat</button>
      </div>
      <div class="summary" id="sum-a" style="display:none"></div>
    </div>

    <!-- Istanbul card -->
    <div class="pc">
      <div class="pc-head">
        <div class="pc-icon">🌉</div>
        <div>
          <div class="pc-title">İstanbul</div>
          <div class="pc-sub">3 dk · 16:9 Yatay · Manzara</div>
        </div>
      </div>
      <div class="pc-row">
        <div class="pc-label">Klip sayısı</div>
        <input class="cnt-in" type="number" id="cnt-i" value="2" min="1" max="10">
      </div>
      <div class="pc-row">
        <button class="rec-btn ist" id="rbtn-i" onclick="record('istanbul')">▶ Kaydet</button>
        <div class="rec-busy"><div class="dot" id="dot-i"></div><span id="st-i">Hazır</span></div>
      </div>
      <div class="daemon-row">
        <div class="daemon-lbl">Otomatik (daemon)</div>
        <button class="dtoggle off" id="dtoggle-i" onclick="toggleDaemon('istanbul')">Başlat</button>
      </div>
      <div class="summary" id="sum-i" style="display:none"></div>
    </div>

    <div style="flex:1"></div>
    <div style="padding:10px 14px;font-size:10px;color:var(--txt3);line-height:1.7">
      <div id="cam-count">Ankara canlı kamera: —</div>
      <div>İstanbul kamera: 21</div>
    </div>

  </div>

  <!-- CLIP AREA -->
  <div class="cliparea">
    <div class="clip-toolbar">
      <div class="clip-filter">
        <button class="cf ca" id="cf-all"  onclick="filterClips('all')">Tümü</button>
        <button class="cf"    id="cf-anka" onclick="filterClips('ankara')">🚌 Ankara</button>
        <button class="cf"    id="cf-ist"  onclick="filterClips('istanbul')">🌉 İstanbul</button>
      </div>
      <div class="tb-spacer"></div>
      <div class="sel-actions">
        <span class="sel-lbl" id="sel-lbl">0 seçili</span>
        <button class="act-btn" onclick="selectAll()">Tümünü Seç</button>
        <button class="act-btn del" onclick="deleteSelected()">Seçilenleri Sil</button>
        <button class="upl-btn" id="upl-btn" onclick="openConfirm()" disabled>
          ▶ YouTube'a Gönder <span id="upl-count">(0)</span>
        </button>
      </div>
    </div>
    <div class="clist" id="clist"></div>
  </div>

</div>

<!-- LOG PANEL -->
<div class="logpanel">
  <div class="logtabs">
    <button class="ltab la" id="ltab-a" onclick="selLog('a')">Ankara Log</button>
    <button class="ltab"    id="ltab-i" onclick="selLog('i')">İstanbul Log</button>
    <button class="ltab"    id="ltab-u" onclick="selLog('u')">Yükleme</button>
    <div class="log-spacer"></div>
    <button class="log-clr" onclick="clearLog()">Temizle</button>
  </div>
  <div class="logbody" id="log-a"></div>
  <div class="logbody" id="log-i" style="display:none"></div>
  <div class="logbody" id="log-u" style="display:none"></div>
</div>

<!-- UPLOAD CONFIRM MODAL -->
<div class="ov" id="upl-ov">
  <div class="modal">
    <div class="modal-head">
      <span>YouTube'a Gönder</span>
      <button class="xbtn" onclick="closeConfirm()">✕</button>
    </div>
    <div class="modal-list" id="modal-list"></div>
    <div class="modal-warn" id="modal-warn"></div>
    <div class="modal-foot">
      <button class="mbtn cancel" onclick="closeConfirm()">İptal</button>
      <button class="mbtn confirm" id="confirm-btn" onclick="doUpload()">✓ Yükle</button>
    </div>
  </div>
</div>

<!-- VIDEO MODAL -->
<div class="vov" id="vov" onclick="closeVideo(event)">
  <div class="vbox">
    <button class="xbtn" style="position:absolute;top:8px;right:10px" onclick="closeVideo()">✕</button>
    <div class="vname" id="vname"></div>
    <video id="vplayer" controls autoplay playsinline></video>
  </div>
</div>

<script>
const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// ── state ──
let allClips = [];
let curFilter = 'all';
let curLog = 'a';
let pendingUploads = [];
let esAnka = null, esIst = null, esUpl = null;

// ── log ──
function parseLine(r) {
  const m = r.match(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ (INFO|WARNING|ERROR)\s+(.*)$/);
  if (!m) return {c:'m', t:r};
  const [,lv,t] = m;
  return {
    c: lv==='WARNING'?'w': lv==='ERROR'?'e':
       /HAZIR|TAMAM|ses eklen|basarili|yuklendi/i.test(t)?'ok':'i',
    t
  };
}
function logLine(raw, el) {
  const b = document.getElementById(el);
  if (!b) return;
  const {c,t} = parseLine(raw);
  const d = document.createElement('div');
  d.className = c; d.textContent = t;
  b.appendChild(d);
  b.scrollTop = b.scrollHeight;
}
function selLog(k) {
  curLog = k;
  ['a','i','u'].forEach(x => {
    document.getElementById('log-'+x).style.display = x===k?'':'none';
    const tab = document.getElementById('ltab-'+x);
    tab.className = 'ltab' + (x===k?' l'+x:'');
  });
}
function clearLog() {
  document.getElementById('log-'+curLog).innerHTML = '';
}

// ── record ──
function setRecState(pipe, busy) {
  const key = pipe==='ankara'?'a':'i';
  document.getElementById('rbtn-'+key).disabled = busy;
  document.getElementById('dot-'+key).className = 'dot'+(busy?' spin':' ok');
  document.getElementById('st-'+key).textContent = busy?'Kaydediyor...':'Hazır';
  if (!busy) { loadClips(); loadStats(); }
}

function record(pipe) {
  const key = pipe==='ankara'?'a':'i';
  const count = document.getElementById('cnt-'+key).value;
  const logEl = 'log-'+key;
  selLog(key);
  setRecState(pipe, true);
  document.getElementById('sum-'+key).style.display = 'none';

  const es = new EventSource(`/api/record/${pipe}?count=${count}`);
  if (pipe==='ankara') esAnka = es; else esIst = es;

  es.onmessage = e => {
    if (e.data === '__DONE__') {
      es.close();
      setRecState(pipe, false);
      loadSummary(pipe);
    } else {
      logLine(e.data, logEl);
    }
  };
  es.onerror = () => { es.close(); setRecState(pipe, false); };
}

function loadSummary(pipe) {
  const key = pipe==='ankara'?'a':'i';
  const el  = document.getElementById('sum-'+key);
  const log = document.getElementById('log-'+key);
  if (!log) return;
  const lines = Array.from(log.children).map(d => d.textContent);
  const tamam = lines.find(l => /KAYIT TAMAM/.test(l));
  if (tamam) {
    el.textContent = tamam.replace(/.*KAYIT TAMAM:\s*/,'');
    el.className = 'summary ok';
  } else {
    const warn = lines.filter(l => /atlaniyor|alinamadi/i.test(l)).length;
    el.textContent = warn > 0 ? `${warn} kamera atlandı` : 'Tamamlandı';
    el.className = 'summary warn';
  }
  el.style.display = '';
}

// ── daemon ──
const daemonState = {ankara: false, istanbul: false};
function toggleDaemon(pipe) {
  const key  = pipe==='ankara'?'a':'i';
  const btn  = document.getElementById('dtoggle-'+key);
  const isOn = daemonState[pipe];
  fetch('/api/daemon/'+(isOn?'stop':'start')+'/'+pipe, {method:'POST'})
    .then(r=>r.json()).then(d => {
      daemonState[pipe] = d.running;
      btn.textContent = d.running ? 'Durdur' : 'Başlat';
      btn.className   = 'dtoggle ' + (d.running?'on':'off');
    });
}

// ── clips ──
function filterClips(f) {
  curFilter = f;
  ['all','anka','ist'].forEach(x => {
    const el = document.getElementById('cf-'+x);
    const active = (x==='all'&&f==='all')||(x==='anka'&&f==='ankara')||(x==='ist'&&f==='istanbul');
    el.className = 'cf' + (active?(x==='anka'?' ca':x==='ist'?' ci':' ca'):'');
  });
  renderClips();
}

function loadClips() {
  fetch('/api/clips').then(r=>r.json()).then(d => {
    allClips = d.clips;
    renderClips();
    updateSelCount();
  });
}

function renderClips() {
  const el = document.getElementById('clist');
  const filtered = allClips.filter(c =>
    curFilter==='all' || c.city === curFilter
  );
  if (!filtered.length) {
    el.innerHTML = '<div class="empty">Henüz klip yok — sol panelden "Kaydet"e bas</div>';
    return;
  }
  el.innerHTML = filtered.map((c,i) => {
    const city   = c.city==='ankara'?'a':'i';
    const dir    = c.city;
    const niceName = c.name.replace(/_/g,' ').replace('.mp4','');
    const yt     = c.youtube_url ? `<a class="yt-link" href="${esc(c.youtube_url)}" target="_blank">▶ YT</a>` : '';
    const cbDisabled = c.uploaded ? 'disabled' : '';
    const rowCls = c.uploaded ? 'crow uploaded' : 'crow';
    return `<div class="${rowCls}" data-name="${esc(c.name)}" data-city="${esc(c.city)}" data-title="${esc(c.title)}">
      <input type="checkbox" ${cbDisabled} onchange="updateSelCount()" ${c.uploaded?'':''}
             data-name="${esc(c.name)}" data-city="${esc(c.city)}" data-title="${esc(c.title)}" data-mb="${c.size_mb}">
      <span class="ctag ${city}">${c.city==='ankara'?'🚌 AKA':'🌉 İST'}</span>
      <div class="cinfo" onclick="playVideo('${esc(c.name)}','${esc(c.city)}')">
        <div class="cn">${esc(niceName)}</div>
        <div class="ctitle">${esc(c.title)}</div>
        <div class="cm">
          <span>${c.modified}</span>
          <span>${c.size_mb} MB</span>
          ${c.uploaded ? '<span style="color:var(--grn)">✓ Yüklendi</span>' : ''}
        </div>
      </div>
      ${yt}
      <button class="ib" onclick="playVideo('${esc(c.name)}','${esc(c.city)}')">▶</button>
      <button class="ib" onclick="deleteClip('${esc(c.name)}','${esc(c.city)}',this)">✕</button>
    </div>`;
  }).join('');
}

function updateSelCount() {
  const cbs = Array.from(document.querySelectorAll('#clist input[type=checkbox]:checked'));
  const n = cbs.length;
  document.getElementById('sel-lbl').textContent = n + ' seçili';
  document.getElementById('upl-count').textContent = '(' + n + ')';
  document.getElementById('upl-btn').disabled = n === 0;
}

function selectAll() {
  document.querySelectorAll('#clist input[type=checkbox]:not(:disabled)').forEach(cb => cb.checked = true);
  updateSelCount();
}

function deleteSelected() {
  const cbs = Array.from(document.querySelectorAll('#clist input[type=checkbox]:checked'));
  if (!cbs.length) return;
  if (!confirm(cbs.length + ' klip silinsin mi?')) return;
  Promise.all(cbs.map(cb =>
    fetch('/clips/'+cb.dataset.city+'/'+encodeURIComponent(cb.dataset.name), {method:'DELETE'})
  )).then(() => loadClips());
}

function deleteClip(name, city, btn) {
  if (!confirm(name + ' silinsin mi?')) return;
  fetch('/clips/'+city+'/'+encodeURIComponent(name), {method:'DELETE'})
    .then(r=>r.json()).then(d => { if(d.ok) { btn.closest('.crow').remove(); loadClips(); }});
}

// ── upload confirm ──
function openConfirm() {
  const cbs = Array.from(document.querySelectorAll('#clist input[type=checkbox]:checked'));
  pendingUploads = cbs.map(cb => ({name: cb.dataset.name, city: cb.dataset.city, title: cb.dataset.title, mb: cb.dataset.mb}));
  if (!pendingUploads.length) return;

  document.getElementById('modal-list').innerHTML = pendingUploads.map((u,i) => `
    <div class="modal-item">
      <div class="mt">${i+1}. ${esc(u.title)}</div>
      <div class="ms">
        <span>${u.city==='ankara'?'🚌 Ankara':'🌉 İstanbul'}</span>
        <span>${u.mb} MB</span>
      </div>
    </div>`).join('');

  fetch('/api/stats').then(r=>r.json()).then(d => {
    const used = d.yt_used;
    const after = used + pendingUploads.length;
    const warn = document.getElementById('modal-warn');
    if (after > 6) {
      warn.textContent = `⚠ Günlük kota aşılacak: ${used}/6 kullanıldı, ${pendingUploads.length} ekleniyor.`;
      warn.style.display = '';
    } else {
      warn.textContent = `Yükleme sonrası kota: ${after}/6`;
      warn.style.display = '';
    }
  });
  document.getElementById('upl-ov').classList.add('open');
}
function closeConfirm() {
  document.getElementById('upl-ov').classList.remove('open');
}
function doUpload() {
  closeConfirm();
  selLog('u');
  document.getElementById('upl-btn').disabled = true;
  document.getElementById('top-status').textContent = 'Yukluyor...';

  const body = JSON.stringify({clips: pendingUploads});
  esUpl = new EventSource('/api/upload/stream');

  // POST once, then listen
  fetch('/api/upload', {method:'POST', headers:{'Content-Type':'application/json'}, body})
    .then(() => {});

  esUpl.onmessage = e => {
    const raw = e.data;
    if (raw === '__DONE__') {
      esUpl.close();
      document.getElementById('upl-btn').disabled = false;
      document.getElementById('top-status').textContent = '';
      loadClips(); loadStats();
    } else {
      const cls = raw.startsWith('__OK__')?'ok': raw.startsWith('__ERR__')?'e':'i';
      const text = raw.replace(/^__\w+__ /, '');
      const b = document.getElementById('log-u');
      const d = document.createElement('div');
      d.className = cls; d.textContent = text;
      b.appendChild(d); b.scrollTop = b.scrollHeight;
    }
  };
  esUpl.onerror = () => { esUpl.close(); loadClips(); loadStats(); };
}

// ── video ──
function playVideo(name, city) {
  document.getElementById('vname').textContent = name;
  const v = document.getElementById('vplayer');
  v.src = '/clips/'+city+'/'+encodeURIComponent(name);
  v.play();
  document.getElementById('vov').classList.add('open');
}
function closeVideo(e) {
  if (e && !e.target.classList.contains('vov') && !e.target.classList.contains('xbtn')) return;
  const v = document.getElementById('vplayer');
  v.pause(); v.src = '';
  document.getElementById('vov').classList.remove('open');
}

// ── stats ──
function loadStats() {
  fetch('/api/stats').then(r=>r.json()).then(d => {
    const pct = Math.min(100, (d.yt_used/6)*100);
    const fill = document.getElementById('qfill');
    fill.style.width = pct+'%';
    fill.className = 'qfill' + (pct>=100?' full': pct>=66?' warn':'');
    document.getElementById('quota-num').textContent = d.yt_used+'/6';
    document.getElementById('next-run').textContent = d.next_run;
    document.getElementById('cam-count').textContent = 'Ankara canlı: '+(d.ankara_cams>=0?d.ankara_cams:'?');

    // daemon buttons
    ['ankara','istanbul'].forEach(p => {
      const k = p==='ankara'?'a':'i';
      daemonState[p] = d['daemon_'+p];
      const btn = document.getElementById('dtoggle-'+k);
      btn.textContent = d['daemon_'+p]?'Durdur':'Başlat';
      btn.className = 'dtoggle '+(d['daemon_'+p]?'on':'off');
    });
  });
}

// ── init ──
fetch('/api/logs/ankara').then(r=>r.json()).then(d => d.lines.slice(-80).forEach(l => logLine(l,'log-a')));
fetch('/api/logs/istanbul').then(r=>r.json()).then(d => d.lines.slice(-80).forEach(l => logLine(l,'log-i')));
loadClips();
loadStats();
setInterval(loadStats, 20000);
setInterval(loadClips, 30000);
</script>
</body>
</html>
"""


# ── upload SSE state ──────────────────────────────────────────────────────────
_upl_pending: list = []
_upl_lock = threading.Lock()


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(TMPL)


@app.route("/api/record/<pipeline>")
def api_record(pipeline):
    if pipeline not in ("ankara", "istanbul"):
        return "Not found", 404
    count = int(request.args.get("count", 1))
    if not _rec_run[pipeline].is_set():
        threading.Thread(target=_do_record, args=(pipeline, count), daemon=True).start()

    q = _rec_q[pipeline]
    def generate():
        while True:
            try:
                line = q.get(timeout=120)
                yield f"data: {line}\n\n"
                if line == "__DONE__": break
            except queue.Empty:
                yield "data: \n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    global _upl_pending
    clips = request.json.get("clips", [])
    with _upl_lock:
        _upl_pending = clips
    if not _upl_run.is_set():
        threading.Thread(target=_do_upload, args=(clips,), daemon=True).start()
    return jsonify({"ok": True, "count": len(clips)})


@app.route("/api/upload/stream")
def api_upload_stream():
    def generate():
        while True:
            try:
                line = _upl_q.get(timeout=300)
                yield f"data: {line}\n\n"
                if line == "__DONE__": break
            except queue.Empty:
                yield "data: \n\n"
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.route("/api/daemon/<action>/<pipeline>", methods=["POST"])
def api_daemon(action, pipeline):
    global _daemon
    if pipeline not in ("ankara", "istanbul"):
        return jsonify({"error": "unknown pipeline"}), 400

    if action == "start":
        if _daemon[pipeline] is None or _daemon[pipeline].poll() is not None:
            script = "main.py" if pipeline == "ankara" else "istanbul_main.py"
            _daemon[pipeline] = subprocess.Popen(
                [sys.executable, script, "--daemon"],
                cwd=str(Path(__file__).parent)
            )
        return jsonify({"running": True})

    elif action == "stop":
        if _daemon[pipeline] and _daemon[pipeline].poll() is None:
            _daemon[pipeline].terminate()
            try: _daemon[pipeline].wait(timeout=5)
            except Exception: _daemon[pipeline].kill()
        _daemon[pipeline] = None
        return jsonify({"running": False})

    return jsonify({"error": "unknown action"}), 400


@app.route("/api/stats")
def api_stats():
    cfg = _cfg()
    times = cfg["schedule"]["times"]
    yt_a = _yt_today(LOG_PATH)
    yt_i = _yt_today(IST_LOG)
    return jsonify({
        "yt_used":      yt_a + yt_i,
        "next_run":     _next_run(times),
        "ankara_cams":  -1,  # expensive, loaded separately
        "daemon_ankara":   _daemon["ankara"] is not None and _daemon["ankara"].poll() is None,
        "daemon_istanbul": _daemon["istanbul"] is not None and _daemon["istanbul"].poll() is None,
    })


@app.route("/api/stats/cams")
def api_stats_cams():
    return jsonify({"ankara_cams": _ankara_cam_count()})


@app.route("/api/clips")
def api_clips():
    a = _clips_with_meta(CLIPS_DIR)
    i = _clips_with_meta(IST_DIR)
    all_clips = sorted(a + i, key=lambda x: x["modified"], reverse=True)
    return jsonify({"clips": all_clips})


@app.route("/api/logs/<pipeline>")
def api_logs(pipeline):
    path = LOG_PATH if pipeline == "ankara" else IST_LOG
    return jsonify({"lines": _tail(path, 200)})


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
    meta = path.with_suffix(".meta.json")
    if meta.exists(): meta.unlink()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("AsfaltTV Dashboard -> http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
