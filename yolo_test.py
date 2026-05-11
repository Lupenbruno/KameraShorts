"""Ankara Pipeline Test Paneli — kayıt → YOLO → geocoder → başlık → hava → ses/overlay → önizleme."""
import os, queue, random, shutil, subprocess, sys, tempfile, threading
from datetime import datetime
from pathlib import Path

import yaml
from flask import Blueprint, Response, render_template_string, send_file

yolo_bp = Blueprint("yolo_test", __name__)

CONFIG_PATH = Path("config.yaml")
TEST_DIR    = Path("data/yolo_test")
TEST_DIR.mkdir(parents=True, exist_ok=True)

_NW = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

COCO = {0:"person",1:"bicycle",2:"car",3:"motorcycle",5:"bus",7:"truck",
        9:"traffic light",11:"stop sign",13:"bench",56:"chair",60:"table"}

# ─── HTML ─────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<title>Ankara Pipeline Test</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d0d0d; color: #e0e0e0; font-family: 'Segoe UI', sans-serif; padding: 24px; }
h1   { color: #00e5ff; font-size: 1.4rem; margin-bottom: 20px; }

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
  height: 420px; overflow-y: auto;
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

/* ─── Video + metadata paneli ─── */
#result-wrap { margin-top: 28px; display: none; }

.result-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 20px;
}
@media (max-width: 900px) { .result-grid { grid-template-columns: 1fr; } }

.vid-card {
  background: #161616;
  border: 1px solid #2a2a2a;
  border-radius: 10px;
  padding: 14px;
}
.vid-card h3 { font-size: .95rem; margin-bottom: 10px; color: #aaa; }
.vid-card h3 span { font-weight: 700; }
.vid-card video {
  width: auto; height: 420px;
  max-width: 100%;
  border-radius: 6px;
  border: 2px solid #2a2a2a;
  display: block;
  margin: 0 auto;
  background: #000;
}
.vid-card.pass h3 span { color: #69ff47; }
.vid-card.fail h3 span { color: #ff5252; }
.vid-card.final h3 span { color: #00e5ff; }

/* ─── Metadata kartı ─── */
#meta-card {
  background: #161616;
  border: 1px solid #2a2a2a;
  border-radius: 10px;
  padding: 18px;
  margin-top: 4px;
}
#meta-card h3 { color: #00e5ff; font-size: 1rem; margin-bottom: 14px; }
.meta-row { margin-bottom: 10px; }
.meta-label {
  font-size: .72rem; color: #616161; text-transform: uppercase;
  letter-spacing: .06em; margin-bottom: 3px;
}
.meta-value {
  font-size: .9rem; color: #e0e0e0;
  background: #0d0d0d; border-radius: 4px;
  padding: 8px 10px; border: 1px solid #222;
  white-space: pre-wrap; word-break: break-word;
}
.meta-value.title-val { color: #ffffff; font-weight: 600; font-size: .95rem; }
.tags-wrap { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.tag {
  background: #1e1e1e; border: 1px solid #333;
  border-radius: 20px; padding: 3px 10px;
  font-size: .78rem; color: #aaa;
}
.weather-badge {
  display: inline-block;
  background: #1a2a3a; border: 1px solid #00bcd4;
  border-radius: 20px; padding: 4px 12px;
  font-size: .85rem; color: #00e5ff;
  margin-bottom: 12px;
}
.verdict-banner {
  text-align: center; padding: 12px;
  border-radius: 8px; font-size: 1rem; font-weight: 700;
  margin-bottom: 16px;
}
.verdict-pass { background: #0d2e12; border: 1px solid #69ff47; color: #69ff47; }
.verdict-fail { background: #2e0d0d; border: 1px solid #ff5252; color: #ff5252; }
</style>
</head>
<body>
<h1>🤖 Ankara Pipeline Test Paneli</h1>
<button id="btn" onclick="startTest()">▶ Tam Pipeline Testi Başlat</button>

<div id="console"><span style="color:#616161">Test başlatmak için butona bas...</span></div>

<div id="result-wrap">
  <div id="verdict-banner" class="verdict-banner"></div>

  <div class="result-grid">
    <div class="vid-card" id="raw-card">
      <h3>📹 Ham Kayıt &nbsp;<span id="raw-label"></span></h3>
      <video id="raw-player" controls muted></video>
    </div>
    <div class="vid-card final" id="final-card" style="display:none">
      <h3>✨ Pipeline Çıktısı &nbsp;<span style="color:#00e5ff">TTS + Overlay</span></h3>
      <video id="final-player" controls></video>
    </div>
  </div>

  <div id="meta-card" style="display:none">
    <h3>📋 YouTube Metadata Önizlemesi</h3>
    <div id="weather-badge-wrap"></div>
    <div class="meta-row">
      <div class="meta-label">Başlık</div>
      <div class="meta-value title-val" id="m-title"></div>
    </div>
    <div class="meta-row">
      <div class="meta-label">Açıklama</div>
      <div class="meta-value" id="m-desc" style="max-height:140px;overflow-y:auto;font-size:.82rem"></div>
    </div>
    <div class="meta-row">
      <div class="meta-label">Etiketler</div>
      <div class="tags-wrap" id="m-tags"></div>
    </div>
  </div>
</div>

<script>
let rawFile = null, finalFile = null;

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
    .replace(/(Skor filtresi|YOLO ANALİZİ|Model:|🌤️ Hava|📍 Konum|🎬 Başlık)[^\n]*/g,
             m => `<span class="info">${m}</span>`);
}

function startTest() {
  const btn = document.getElementById('btn');
  const con = document.getElementById('console');
  btn.disabled = true;
  con.innerHTML = '';
  document.getElementById('result-wrap').style.display = 'none';
  document.getElementById('meta-card').style.display = 'none';
  document.getElementById('final-card').style.display = 'none';
  rawFile = finalFile = null;

  const es = new EventSource('/yolo-test/run');

  es.onmessage = e => {
    const msg = e.data;

    if (msg === '__DONE__') { es.close(); btn.disabled = false; return; }

    // Ham video (YOLO sonrası)
    if (msg.startsWith('__RAW_PASS__:') || msg.startsWith('__RAW_FAIL__:')) {
      const passed = msg.startsWith('__RAW_PASS__:');
      rawFile = msg.slice(msg.indexOf(':') + 1);
      const label = passed ? '✅ YOLO Geçti' : '❌ YOLO Eledi';
      document.getElementById('raw-label').textContent = label;
      document.getElementById('raw-player').src = '/yolo-test/video/' + rawFile;
      const rc = document.getElementById('raw-card');
      rc.className = 'vid-card ' + (passed ? 'pass' : 'fail');
      document.getElementById('result-wrap').style.display = 'block';
      const banner = document.getElementById('verdict-banner');
      if (passed) {
        banner.className = 'verdict-banner verdict-pass';
        banner.textContent = '✅ YOLO GEÇTİ — Pipeline çıktısı hazırlanıyor...';
      } else {
        banner.className = 'verdict-banner verdict-fail';
        banner.textContent = '❌ YOLO ELEDİ — Bu video yüklenmeyecek';
      }
      return;
    }

    // İşlenmiş final video
    if (msg.startsWith('__FINAL__:')) {
      finalFile = msg.slice(10);
      document.getElementById('final-player').src = '/yolo-test/video/' + finalFile;
      document.getElementById('final-card').style.display = 'block';
      document.getElementById('verdict-banner').textContent = '✅ YOLO GEÇTİ — Pipeline çıktısı hazır!';
      return;
    }

    // Metadata JSON
    if (msg.startsWith('__META__:')) {
      try {
        const meta = JSON.parse(msg.slice(9));
        document.getElementById('m-title').textContent = meta.title || '';
        document.getElementById('m-desc').textContent  = meta.description || '';
        const tagsWrap = document.getElementById('m-tags');
        tagsWrap.innerHTML = '';
        (meta.tags || []).forEach(t => {
          const s = document.createElement('span');
          s.className = 'tag'; s.textContent = '#' + t;
          tagsWrap.appendChild(s);
        });
        if (meta.weather) {
          document.getElementById('weather-badge-wrap').innerHTML =
            `<div class="weather-badge">${meta.weather}</div>`;
        }
        document.getElementById('meta-card').style.display = 'block';
      } catch(e) {}
      return;
    }

    con.innerHTML += col(msg) + '\n';
    con.scrollTop = con.scrollHeight;
  };

  es.onerror = () => {
    es.close(); btn.disabled = false;
    con.innerHTML += '<span class="err">Bağlantı kesildi.</span>\n';
  };
}
</script>
</body>
</html>"""


# ─── Worker ───────────────────────────────────────────────────────────────────
def _run(q: queue.Queue):
    def put(msg): q.put(msg)

    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        ff  = cfg.get("ffmpeg_path") or ""
        if ff and not Path(ff).exists(): ff = ""
        ff  = ff or shutil.which("ffmpeg") or "ffmpeg"
        now = datetime.now()

        put("━" * 52)
        put("  🎯  ANKARA PIPELINE TEST")
        put(f"  ⏰  {now.strftime('%d/%m/%Y %H:%M:%S')}")
        put("━" * 52)

        # ── 1. Kameralar ──────────────────────────────────────────────────────
        put("\n📡  Aktif Ankara kameraları çekiliyor...")
        from src.camera_registry import CameraRegistry
        cams = CameraRegistry().get_active_cameras()
        put(f"  {len(cams)} aktif kamera")

        TYPE_PRIORITY = {"Solo": 0, "Körüklü": 1, "ELK": 2}
        # Önce türe göre grupla, sonra her grup içinde karıştır
        solo    = [c for c in cams if (c.get("vehicle_type") or "").strip() == "Solo"]
        korklu  = [c for c in cams if (c.get("vehicle_type") or "").strip() == "Körüklü"]
        elk     = [c for c in cams if (c.get("vehicle_type") or "").strip() == "ELK"]
        diger   = [c for c in cams if (c.get("vehicle_type") or "").strip() not in TYPE_PRIORITY]
        random.shuffle(solo); random.shuffle(korklu)
        random.shuffle(elk);  random.shuffle(diger)
        sorted_cams = solo + korklu + elk + diger

        put(f"\n📋  İlk 15 Kamera (rastgele karıştırıldı)")
        put("─" * 56)
        for c in sorted_cams[:15]:
            plate = c.get("license_plate", "?")
            vtype = (c.get("vehicle_type") or "?").strip()
            grp   = (c.get("group_name") or "")[:28]
            put(f"  [{plate:<12}]  {vtype:<20}  {grp}")
        if len(sorted_cams) > 15:
            put(f"  ... +{len(sorted_cams)-15} kamera daha")
        put("─" * 56)

        # ── 2. Stream seç ─────────────────────────────────────────────────────
        put(f"\n📡  Stream deneniyor...")
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
            put("  ❌  Hiçbir kamera açılamadı.")
            return

        plate  = selected.get("license_plate", "?")
        vtype  = (selected.get("vehicle_type") or "?").strip()
        lat    = float(selected.get("latitude", 39.9334) or 39.9334)
        lon    = float(selected.get("longitude", 32.8597) or 32.8597)

        # ── 3. Klip kaydet ────────────────────────────────────────────────────
        put(f"\n🎥  Klip kaydediliyor  (20 saniye)")
        put(f"  Araç   : {plate}  [{vtype}]")
        stream_url = selected.get("stream_url", "")
        put(f"  Stream : ...{stream_url[-55:]}")

        ts       = now.strftime("%Y%m%d_%H%M%S")
        raw_path = TEST_DIR / f"test_{ts}_raw.mp4"

        with tempfile.TemporaryDirectory() as tmp:
            segs = rec._download_segments(stream_url, 20, tmp)
            if not segs:
                put("  ❌  Segment indirilemedi.")
                return
            put(f"  ✅  {len(segs)} segment indirildi")

            concat = os.path.join(tmp, "c.txt")
            with open(concat, "w") as f:
                for s in segs: f.write(f"file '{s}'\n")

            vf  = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
            cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", concat,
                   "-t", "20", "-c:v", "libx264", "-preset", "ultrafast",
                   "-crf", "26", "-c:a", "aac", "-movflags", "+faststart",
                   "-vf", vf, str(raw_path)]
            r = subprocess.run(cmd, capture_output=True, timeout=120, **_NW)

        if r.returncode != 0 or not raw_path.exists():
            put(f"  ❌  Encode başarısız")
            return

        sz = raw_path.stat().st_size / 1024 / 1024
        put(f"  ✅  Ham video: {raw_path.name}  ({sz:.1f} MB)")

        # ── 4. YOLO analizi ───────────────────────────────────────────────────
        put(f"\n{'═'*52}")
        put("  🤖  YOLO ANALİZİ")
        put(f"{'═'*52}")

        from src.ai_filter import (_load_model, OBJECT_SCORES, CONF_THRESH,
                                    MIN_SCORE, _VF_YOLO, _brightness, _dynamic_min_score)
        yolo_ok = _load_model()

        geçti = True
        total = 0
        PANEL_FACTOR   = 3   # 5 kare analiz, dinamik eşiği 3x alıyoruz
        PANEL_MIN_SCORE = MIN_SCORE * PANEL_FACTOR  # başlangıç, ilk kareden güncellenir

        if yolo_ok:
            from src.ai_filter import _model, _sky_bonus
            step       = max(2, 20 // 6)
            timestamps = [step, step*2, step*3, step*4, step*5]
            sky_pts    = 0
            first_brightness = 128.0

            for i, t in enumerate(timestamps):
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as fp_obj:
                    fp = fp_obj.name
                try:
                    subprocess.run(
                        [ff, "-y", "-ss", str(t), "-i", str(raw_path),
                         "-frames:v", "1", "-q:v", "3", "-vf", _VF_YOLO, fp],
                        capture_output=True, timeout=10, **_NW)

                    if not Path(fp).exists():
                        put(f"  Kare {i+1}  t={t}s  → ❌ çekilemedi")
                        continue

                    # İlk kareden parlaklık ölç → dinamik eşik
                    if i == 0:
                        first_brightness = _brightness(fp)
                        dyn_min = _dynamic_min_score(first_brightness)
                        PANEL_MIN_SCORE = dyn_min * PANEL_FACTOR
                        mode = ("🌙 gece" if first_brightness < 60
                                else "🌆 alacakaranlık" if first_brightness < 100
                                else "☀️ gündüz")
                        put(f"  💡 Parlaklık: {first_brightness:.0f}  →  {mode}  →  eşik: {PANEL_MIN_SCORE}p")

                    results = _model(fp, conf=CONF_THRESH, verbose=False)
                    fs, dets = 0, []
                    for res in results:
                        for cls_id, conf in zip(
                            res.boxes.cls.tolist()  if res.boxes else [],
                            res.boxes.conf.tolist() if res.boxes else []
                        ):
                            cls_id = int(cls_id)
                            pts    = OBJECT_SCORES.get(cls_id, 0)
                            fs    += pts
                            dets.append(f"{COCO.get(cls_id, f'cls{cls_id}')}({conf:.0%}+{pts}p)")

                    if i == 0:
                        sky_pts = _sky_bonus(fp)
                        if sky_pts:
                            dets.append(f"🌤️ gökyüzü(+{sky_pts}p)")
                            fs += sky_pts

                    total += fs
                    icon   = "✅" if fs >= 1 else "❌"
                    put(f"  Kare {i+1}  t={t:>2}s  {icon}  {', '.join(dets) or '— tespit yok':<55}  +{fs}p")
                finally:
                    try: os.unlink(fp)
                    except: pass

            geçti = total >= PANEL_MIN_SCORE
            put(f"\n  TOPLAM SKOR : {total} puan  (eşik: {PANEL_MIN_SCORE})")
            put(f"  KARAR: {'✅ GEÇTİ — pipeline yükler' if geçti else '❌ ELENDİ — kalite yetersiz'}")
            put(f"{'═'*52}")
        else:
            put("⚠️  YOLO yüklü değil — geçildi")

        # Ham videoyu panele gönder (geçti / elendi etiketi)
        q.put(f"__RAW_PASS__:{raw_path.name}" if geçti else f"__RAW_FAIL__:{raw_path.name}")

        if not geçti:
            return

        # ── 5. Geocoder ───────────────────────────────────────────────────────
        put(f"\n📍  Konum belirleniyor...  ({lat:.4f}, {lon:.4f})")
        try:
            from src.geocoder import Geocoder
            location = Geocoder().get_location_name(lat, lon)
        except Exception:
            location = "Ankara"
        put(f"  📍 Konum: {location}")

        # ── 6. Hava durumu ────────────────────────────────────────────────────
        owm_key = cfg.get("openweathermap_api_key", "")
        weather = None
        if owm_key:
            put(f"\n🌤️  Hava durumu çekiliyor...")
            try:
                from src.weather import get_weather
                weather = get_weather("ankara", api_key=owm_key)
                if weather:
                    put(f"  🌤️ Hava: {weather['emoji']} {weather['temp']}°C — {weather['condition']}")
                else:
                    put("  ⚠️  OWM yanıt vermedi (key henüz aktif değil?)")
            except Exception as e:
                put(f"  ⚠️  Hava hatası: {e}")
        else:
            put("\n⚠️  OWM API key yok — hava verisi atlandı")

        # ── 7. Metadata üret ──────────────────────────────────────────────────
        put(f"\n🎬  Başlık ve metadata üretiliyor...")
        from src.title_generator import TitleGenerator
        titler   = TitleGenerator(cfg)
        metadata = titler.generate(selected, location, now, weather=weather)
        metadata["city"] = "Ankara"
        put(f"  🎬 Başlık: {metadata['title']}")

        # TTS metni
        GUNLER = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
        AYLAR  = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
                  "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]
        tts_text = (f"{location}. "
                    f"{now.day} {AYLAR[now.month-1]} {GUNLER[now.weekday()]}, "
                    f"saat {now.strftime('%H:%M')}.")
        metadata["tts_text"] = tts_text

        # ── 8. TTS + Overlay ─────────────────────────────────────────────────
        put(f"\n🔊  Ses ve overlay ekleniyor...")
        # YOLO tespitlerini TTS'e ekle
        from src.ai_filter import describe_clip
        yolo_desc = describe_clip(str(raw_path), ff, 20)
        if yolo_desc:
            tts_text += f" {yolo_desc}"
            put(f"  🤖 YOLO TTS: {yolo_desc}")
        speed = selected.get("speed", 0)
        if speed:
            tts_text += f" Otobüs {speed} kilometre hızla ilerliyor."
        put(f"  TTS: \"{tts_text[:100]}...\"" if len(tts_text) > 100 else f"  TTS: \"{tts_text}\"")
        if weather:
            put(f"  Overlay: Ankara  |  {weather['condition']}  {weather['temp']}C")

        final_path = TEST_DIR / f"test_{ts}_final.mp4"
        import shutil as _sh
        _sh.copy(str(raw_path), str(final_path))

        from src.audio_mixer import AudioMixer
        mixer      = AudioMixer(cfg)
        final_path_str = mixer.add_audio(str(final_path), metadata, location, weather=weather)
        final_name = Path(final_path_str).name

        sz2 = Path(final_path_str).stat().st_size / 1024 / 1024
        put(f"  ✅  Final video: {final_name}  ({sz2:.1f} MB)")

        # ── 9. Sonuçları gönder ───────────────────────────────────────────────
        q.put(f"__FINAL__:{final_name}")

        # Metadata JSON
        import json as _json
        meta_payload = {
            "title":       metadata.get("title", ""),
            "description": metadata.get("description", ""),
            "tags":        metadata.get("tags", [])[:20],
            "weather":     f"{weather['emoji']} {weather['temp']}°C — {weather['condition']}" if weather else None,
        }
        q.put(f"__META__:{_json.dumps(meta_payload, ensure_ascii=False)}")

        put(f"\n{'━'*52}")
        put("  🏆  Pipeline testi tamamlandı!")
        put(f"  Ham video    : {raw_path.name}")
        put(f"  Final video  : {final_name}")
        put(f"  Başlık       : {metadata['title'][:60]}")
        put(f"{'━'*52}")

    except Exception as e:
        import traceback
        q.put(f"❌ Beklenmeyen hata: {e}")
        q.put(traceback.format_exc())
    finally:
        q.put("__DONE__")


# ─── Routes ───────────────────────────────────────────────────────────────────
@yolo_bp.route("/yolo-test")
def page():
    return render_template_string(HTML)


@yolo_bp.route("/yolo-test/run")
def run():
    q = queue.Queue()
    threading.Thread(target=_run, args=(q,), daemon=True).start()

    def stream():
        while True:
            msg = q.get()
            yield f"data: {msg}\n\n"
            if msg == "__DONE__":
                break

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
