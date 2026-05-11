"""YOLO Test Paneli — Ankara kamera seçim sürecini canlı izle."""
import os, queue, shutil, subprocess, sys, tempfile, threading
from datetime import datetime
from pathlib import Path

import yaml
from flask import Blueprint, Response, render_template_string, send_file

yolo_bp = Blueprint("yolo_test", __name__)

CONFIG_PATH = Path("config.yaml")
TEST_DIR    = Path("data/yolo_test")
TEST_DIR.mkdir(parents=True, exist_ok=True)

_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# ─── COCO etiketleri ────────────────────────────────────────────────────────
COCO = {0:"person",1:"bicycle",2:"car",3:"motorcycle",5:"bus",7:"truck",
        9:"traffic light",11:"stop sign",13:"bench",56:"chair",60:"table"}

# ─── HTML ────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<title>YOLO Test</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d0d0d; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; padding: 24px; }
h1  { color: #00e5ff; font-size: 1.4rem; margin-bottom: 20px; }

#btn {
  background: #00e5ff; color: #000; border: none; padding: 12px 32px;
  font-size: 1rem; font-weight: 700; border-radius: 6px; cursor: pointer;
  transition: opacity .2s;
}
#btn:disabled { opacity: .4; cursor: not-allowed; }

#console {
  margin-top: 20px;
  background: #111; border: 1px solid #222;
  border-radius: 8px; padding: 16px;
  font-family: 'Cascadia Code', 'Consolas', monospace;
  font-size: .82rem; line-height: 1.55;
  height: 480px; overflow-y: auto;
  white-space: pre-wrap; word-break: break-all;
}
#console .ok    { color: #69ff47; }
#console .err   { color: #ff5252; }
#console .warn  { color: #ffd740; }
#console .info  { color: #40c4ff; }
#console .bold  { color: #ffffff; font-weight: 700; }
#console .dim   { color: #616161; }
#console .score { color: #e040fb; }
#console .pass  { color: #69ff47; font-weight: 700; }
#console .fail  { color: #ff5252; font-weight: 700; }

#video-wrap { margin-top: 24px; display: none; }
#video-wrap h2 { color: #69ff47; margin-bottom: 12px; font-size: 1.1rem; }
#player { width: 100%; max-width: 420px; border-radius: 8px; border: 2px solid #222; display: block; }
#open-btn {
  margin-top: 10px;
  background: #1b1b1b; color: #00e5ff; border: 1px solid #00e5ff;
  padding: 8px 20px; border-radius: 6px; cursor: pointer; font-size: .9rem;
}
#open-btn:hover { background: #00e5ff22; }
</style>
</head>
<body>
<h1>🤖 YOLO Test Paneli — Ankara</h1>
<button id="btn" onclick="startTest()">▶ Test Et</button>

<div id="console"><span class="dim">Test başlatmak için butona bas...</span></div>

<div id="video-wrap">
  <h2>✅ Geçen Video</h2>
  <video id="player" controls autoplay muted></video>
  <br>
  <button id="open-btn" onclick="openInPlayer()">📂 Medya Oynatıcıda Aç</button>
</div>

<script>
let currentFile = null;

function col(text) {
  return text
    .replace(/✅[^\n]*/g, m => `<span class="ok">${m}</span>`)
    .replace(/❌[^\n]*/g, m => `<span class="err">${m}</span>`)
    .replace(/⚠️[^\n]*/g, m => `<span class="warn">${m}</span>`)
    .replace(/(━+|═+|─+)/g, m => `<span class="dim">${m}</span>`)
    .replace(/🏆[^\n]*/g, m => `<span class="bold">${m}</span>`)
    .replace(/TOPLAM SKOR:[^\n]*/g, m => `<span class="score">${m}</span>`)
    .replace(/KARAR: ✅[^\n]*/g, m => `<span class="pass">${m}</span>`)
    .replace(/KARAR: ❌[^\n]*/g, m => `<span class="fail">${m}</span>`)
    .replace(/(Skor filtresi|YOLO ANALİZİ|Model:)[^\n]*/g, m => `<span class="info">${m}</span>`);
}

function startTest() {
  const btn = document.getElementById('btn');
  const con = document.getElementById('console');
  const wrap = document.getElementById('video-wrap');
  btn.disabled = true;
  con.innerHTML = '';
  wrap.style.display = 'none';
  currentFile = null;

  const es = new EventSource('/yolo-test/run');

  es.onmessage = e => {
    const msg = e.data;
    if (msg === '__DONE__') {
      es.close();
      btn.disabled = false;
      return;
    }
    if (msg.startsWith('__VIDEO_PASS__:') || msg.startsWith('__VIDEO_FAIL__:')) {
      const passed = msg.startsWith('__VIDEO_PASS__:');
      currentFile = msg.slice(msg.indexOf(':') + 1);
      document.getElementById('player').src = '/yolo-test/video/' + currentFile;
      const h2 = document.querySelector('#video-wrap h2');
      if (passed) {
        h2.textContent = '✅ Geçen Video — pipeline bu videoyu yükler';
        h2.style.color = '#69ff47';
        document.getElementById('player').style.border = '2px solid #69ff47';
      } else {
        h2.textContent = '❌ Elenen Video — pipeline bu videoyu ATMAZ';
        h2.style.color = '#ff5252';
        document.getElementById('player').style.border = '2px solid #ff5252';
      }
      wrap.style.display = 'block';
      return;
    }
    con.innerHTML += col(msg) + '\n';
    con.scrollTop = con.scrollHeight;
  };

  es.onerror = () => {
    es.close();
    btn.disabled = false;
    con.innerHTML += '<span class="err">Bağlantı kesildi.</span>\n';
  };
}

function openInPlayer() {
  if (currentFile) {
    fetch('/yolo-test/open/' + currentFile);
  }
}
</script>
</body>
</html>"""


# ─── Worker ──────────────────────────────────────────────────────────────────
def _run(q: queue.Queue):
    def put(msg, nl=True): q.put(msg)

    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        ff  = cfg.get("ffmpeg_path") or ""
        if ff and not Path(ff).exists(): ff = ""
        ff  = ff or shutil.which("ffmpeg") or "ffmpeg"
        now = datetime.now()

        put("━" * 52)
        put("  🎯  YOLO TEST BAŞLIYOR")
        put(f"  ⏰  {now.strftime('%d/%m/%Y %H:%M:%S')}")
        put("━" * 52)

        # 1. Cameras
        put("\n📡  Aktif Ankara kameraları çekiliyor...")
        from src.camera_registry import CameraRegistry
        cams = CameraRegistry().get_active_cameras()
        put(f"  {len(cams)} aktif kamera bulundu")

        # 2. Araç tipine göre önceliklendir (otobüsler önce)
        TYPE_PRIORITY = {"Solo": 0, "Körüklü": 1, "ELK": 2}
        def _priority(c):
            vt = (c.get("vehicle_type") or "").strip()
            return TYPE_PRIORITY.get(vt, 99)

        sorted_cams = sorted(cams, key=_priority)

        put(f"\n📋  Kamera Listesi  (öncelik: otobüs → iş makinesi)")
        put("─" * 56)
        type_counts = {}
        for c in sorted_cams[:20]:
            plate = c.get("license_plate", "?")
            vtype = (c.get("vehicle_type") or "?").strip()
            grp   = (c.get("group_name") or "")[:30]
            type_counts[vtype] = type_counts.get(vtype, 0) + 1
            put(f"  [{plate:<12}]  {vtype:<20}  {grp}")
        if len(sorted_cams) > 20:
            put(f"  ... +{len(sorted_cams)-20} kamera daha")
        put("─" * 56)
        for vt, cnt in sorted(type_counts.items(), key=lambda x: _priority({"vehicle_type":x[0]})):
            put(f"  {vt:<22} → {cnt} adet")

        # 3. Relay dene — ilk çalışan kamerayı seç
        put(f"\n📡  Relay deneniyor  (sırayla, ilk açılan kazanır)...")
        from src.clip_recorder import ClipRecorder
        rec      = ClipRecorder(cfg)
        selected = None
        for cam in sorted_cams[:15]:
            plate = cam.get("license_plate", "?")
            vtype = (cam.get("vehicle_type") or "?").strip()
            put(f"  [{plate}]  {vtype}  → deneniyor...")
            if rec._start_relay(cam):
                selected = cam
                put(f"  ✅  [{plate}] stream hazır!")
                break
            else:
                put(f"  ❌  [{plate}] yanıt yok")

        if not selected:
            put("  ❌  Hiçbir kamera açılamadı. Test sonlandırıldı.")
            return

        plate = selected.get("license_plate", "?")
        vtype = (selected.get("vehicle_type") or "?").strip()

        # 4. Record 20s test clip
        put(f"\n🎥  Klip kaydediliyor  (20 saniye)")
        put(f"  Araç   : {plate}  [{vtype}]")
        stream_url = selected.get("stream_url", "")
        put(f"  Stream : ...{stream_url[-55:]}")

        ts       = now.strftime("%Y%m%d_%H%M%S")
        out_path = TEST_DIR / f"test_{ts}.mp4"

        with tempfile.TemporaryDirectory() as tmp:
            put("  Segmentler indiriliyor...")
            segs = rec._download_segments(stream_url, 20, tmp)
            if not segs:
                put("  ❌  Segment indirilemedi.")
                return
            put(f"  ✅  {len(segs)} segment indirildi")

            concat = os.path.join(tmp, "c.txt")
            with open(concat, "w") as f:
                for s in segs: f.write(f"file '{s}'\n")

            vf  = ("scale=1080:1920:force_original_aspect_ratio=increase,"
                   "crop=1080:1920")
            cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", concat,
                   "-t", "20", "-c:v", "libx264", "-preset", "ultrafast",
                   "-crf", "26", "-c:a", "aac", "-movflags", "+faststart",
                   "-vf", vf, str(out_path)]
            put("  Encode ediliyor...")
            r = subprocess.run(cmd, capture_output=True, timeout=120, **_NW)

        if r.returncode != 0 or not out_path.exists():
            put(f"  ❌  Encode başarısız:\n{r.stderr.decode(errors='replace')[-300:]}")
            return

        sz = out_path.stat().st_size / 1024 / 1024
        put(f"  ✅  Video hazır: {out_path.name}  ({sz:.1f} MB)")

        # 5. YOLO frame analizi
        put(f"\n{'═'*52}")
        put("  🤖  YOLO ANALİZİ")
        put(f"{'═'*52}")

        from src.ai_filter import _load_model, OBJECT_SCORES, CONF_THRESH, MIN_SCORE
        # Test paneli 5 kare analiz ediyor, pipeline sadece 1 kare bakıyor.
        # Eşiği kare sayısıyla orantılı tut (5x).
        PANEL_MIN_SCORE = MIN_SCORE * 5  # 4 × 5 = 20
        yolo_ok = _load_model()

        if not yolo_ok:
            put("⚠️   YOLO yüklü değil — sadece video kaydedildi.")
            q.put(f"__VIDEO_PASS__:{out_path.name}")
            return

        from src.ai_filter import _model, _sky_bonus
        step       = max(2, 20 // 6)
        timestamps = [step, step * 2, step * 3, step * 4, step * 5]
        total      = 0
        sky_pts    = 0

        for i, t in enumerate(timestamps):
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                fp = f.name
            try:
                cmd2 = [ff, "-y", "-ss", str(t), "-i", str(out_path),
                        "-frames:v", "1", "-q:v", "3", "-vf", "scale=640:-1", fp]
                subprocess.run(cmd2, capture_output=True, timeout=10, **_NW)

                if not Path(fp).exists():
                    put(f"  Kare {i+1}  t={t}s  → ❌ çekilemedi")
                    continue

                results = _model(fp, conf=CONF_THRESH, verbose=False)
                fs      = 0
                dets    = []

                for res in results:
                    cls_list  = res.boxes.cls.tolist()  if res.boxes else []
                    conf_list = res.boxes.conf.tolist() if res.boxes else []
                    for cls_id, conf in zip(cls_list, conf_list):
                        cls_id = int(cls_id)
                        pts    = OBJECT_SCORES.get(cls_id, 0)
                        fs    += pts
                        name   = COCO.get(cls_id, f"cls{cls_id}")
                        dets.append(f"{name}({conf:.0%}+{pts}p)")

                # Gökyüzü bonusu — sadece ilk kare için hesapla
                if i == 0:
                    sky_pts = _sky_bonus(fp)
                    if sky_pts:
                        dets.append(f"🌤️ gökyüzü(+{sky_pts}p)")
                        fs += sky_pts

                total += fs
                det_str = ", ".join(dets) if dets else "— tespit yok"
                icon    = "✅" if fs >= 1 else "❌"
                put(f"  Kare {i+1}  t={t:>2}s  {icon}  {det_str:<55}  +{fs}p")
            finally:
                try: os.unlink(fp)
                except: pass

        geçti = total >= PANEL_MIN_SCORE
        put(f"\n  TOPLAM SKOR : {total} puan  (eşik: {PANEL_MIN_SCORE})")
        if geçti:
            put(f"  KARAR: ✅ GEÇTİ — YouTube'a yüklenebilir")
        else:
            put(f"  KARAR: ❌ ELENDİ — Kalite yetersiz (zemin/damper/karanlık)")
        put(f"{'═'*52}")

        q.put(f"__VIDEO_PASS__:{out_path.name}" if geçti else f"__VIDEO_FAIL__:{out_path.name}")

    except Exception as e:
        import traceback
        q.put(f"❌ Beklenmeyen hata: {e}")
        q.put(traceback.format_exc())
    finally:
        q.put("__DONE__")


# ─── Routes ──────────────────────────────────────────────────────────────────
@yolo_bp.route("/yolo-test")
def page():
    return render_template_string(HTML)


@yolo_bp.route("/yolo-test/run")
def run():
    q = queue.Queue()

    def worker():
        _run(q)

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            msg = q.get()
            yield f"data: {msg}\n\n"
            if msg == "__DONE__":
                break

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@yolo_bp.route("/yolo-test/video/<fn>")
def video(fn):
    p = TEST_DIR / fn
    if p.exists():
        return send_file(str(p), mimetype="video/mp4")
    return "Bulunamadı", 404


@yolo_bp.route("/yolo-test/open/<fn>")
def open_player(fn):
    p = TEST_DIR / fn
    if p.exists():
        if sys.platform == "win32":
            os.startfile(str(p))
        else:
            subprocess.Popen(["xdg-open", str(p)])
    return "", 204
